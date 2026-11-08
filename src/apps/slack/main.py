import fcntl
import signal
import threading
import time
from contextlib import ExitStack

from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.socket_mode.builtin import SocketModeClient

from apps.worker.main import run_worker
from apps.worker.reminders import run_reminders
from composition import build, build_clock
from config import Settings
from shared.errors import AgentError


class SocketHealth:
    """Detect a disconnected or unresponsive SDK connection, including a stuck reconnect."""

    def __init__(self, timeout: float = 120) -> None:
        self.timeout = timeout
        self.session = None
        self.session_since = time.monotonic()
        self.disconnected_since: float | None = None

    def failed(self, client: SocketModeClient) -> bool:
        now = time.monotonic()
        session = client.current_session
        if session is not self.session:
            self.session = session
            self.session_since = now
        if not client.is_connected():
            if self.disconnected_since is None:
                self.disconnected_since = now
            return now - self.disconnected_since >= self.timeout
        self.disconnected_since = None
        if session is None:
            return False
        last_pong = session.last_ping_pong_time
        if last_pong is None:
            return now - self.session_since >= self.timeout
        return time.time() - last_pong >= self.timeout


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
            or settings.daily_task_reminder is not None
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
            if reminder := settings.daily_task_reminder:
                print(
                    f"タスクの定期通知: 毎日{reminder.hour:02}:{reminder.minute:02}（日本時間）"
                    f" → {reminder.channel_id}",
                    flush=True,
                )
            health = SocketHealth()
            while not stop.wait(15):
                if health.failed(handler.client):
                    raise AgentError("socket_unhealthy", "Slack接続が復旧しないため再起動します。")
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
