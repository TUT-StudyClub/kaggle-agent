import fcntl
import signal
import threading
from contextlib import ExitStack

from slack_bolt.adapter.socket_mode import SocketModeHandler

from apps.worker.main import run_worker
from apps.worker.reminders import run_reminders
from composition import build, build_clock
from config import Settings
from shared.errors import AgentError


def run(settings: Settings) -> None:
    clock = build_clock()
    settings.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ExitStack() as stack:
        lock = stack.enter_context(settings.database.with_suffix(".lock").open("w"))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentError("already_running", "同じDBを使うプロセスが稼働中です。") from None
        app, agent = build(settings, stack)
        agent.recover()
        stop = threading.Event()
        worker_failed = threading.Event()

        def request_stop(signum, frame):
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.signal(sig, request_stop)
            stack.callback(signal.signal, sig, previous)

        def worker() -> None:
            try:
                run_worker(agent, stop, settings.retention_days, clock)
            except Exception:
                worker_failed.set()
                stop.set()

        thread = threading.Thread(target=worker, name="agent-worker", daemon=True)
        reminder_thread = (
            threading.Thread(
                target=run_reminders, args=(agent, stop), name="schedule-reminders", daemon=True
            )
            if settings.daily_schedule_reminder is not None
            else None
        )
        handler = SocketModeHandler(app, settings.slack_app_token.get_secret_value())
        try:
            handler.connect()
            if not handler.client.is_connected():
                raise AgentError("socket_connection", "SlackのSocket Modeへ接続できませんでした。")
            thread.start()
            if reminder_thread is not None:
                reminder_thread.start()
            print(
                "Slack Socket Modeに接続しました。許可されたDM・メンションを待機しています。",
                flush=True,
            )
            if reminder := settings.daily_schedule_reminder:
                print(
                    f"チーム予定の定期通知: 毎日{reminder.hour:02}:{reminder.minute:02}（日本時間）"
                    f" / {reminder.calendar_alias} → {reminder.channel_id}",
                    flush=True,
                )
            stop.wait()
        finally:
            stop.set()
            handler.close()
            if thread.ident is not None:
                thread.join(timeout=settings.codex_timeout_seconds + 35)
            if reminder_thread is not None and reminder_thread.ident is not None:
                reminder_thread.join(timeout=35)
        if worker_failed.is_set():
            raise AgentError(
                "worker_failed", "ワーカーが停止しました。DBと接続状態を確認してください。"
            )
