import hashlib
import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from modules.knowledge.public import TaskBoardService, TaskNotification
from shared.application.ports.clock import Clock
from shared.errors import AgentError, UncertainWrite

from ...domain.reminders import ReminderRepository
from ..dto.reminders import DailyTaskReminder
from ..ports.reminders import ReminderSender

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")


def task_reminder_text(day: datetime, tasks: list[TaskNotification]) -> str:
    lines = [f"今日のタスク（{day.month}/{day.day}）"]
    for task in sorted(tasks, key=lambda t: (t.title, t.id)):
        title = escape(" ".join(task.title.split()), quote=False)
        lines.append(f"• {title}")
        mentions = " ".join(f"<@{uid}>" for uid in sorted(task.slack_user_ids))
        lines.append(f"  担当者: {mentions or '未設定'}")
    return "\n".join(lines)


class RemindTasksUseCase:
    def __init__(
        self,
        config: DailyTaskReminder,
        team_id: str,
        board: TaskBoardService,
        repository: ReminderRepository,
        sender: ReminderSender,
        clock: Clock,
    ):
        self.config = config
        self.team_id = team_id
        self.board = board
        self.repository = repository
        self.sender = sender
        self.clock = clock
        self._last_error: tuple[str, str] | None = None
        self._retry_at = 0.0

    def execute(self) -> None:
        if self.clock.monotonic() < self._retry_at:
            return
        now = self.clock.now().astimezone(JST)
        if (now.hour, now.minute) < (self.config.hour, self.config.minute):
            return
        identity = (
            f"tasks/{self.team_id}/{self.config.channel_id}/"
            f"{self.config.data_source_id}/{now.date()}"
        )
        key = hashlib.sha256(identity.encode()).hexdigest()
        try:
            state = self.repository.reminder_state(key)
            if state == "sending":
                raise UncertainWrite()
            if state is not None:
                return
            tasks = self.board.tasks_on(now.date())
            text = ""
            if tasks:
                members = self.sender.human_members(self.config.channel_id)
                assigned = set().union(*(t.slack_user_ids for t in tasks))
                if not members or not assigned <= members:
                    raise AgentError(
                        "task_channel_member_missing",
                        "タスク担当者が通知先チャンネルにいるか確認してください。",
                    )
                text = task_reminder_text(now, tasks)
            if self.clock.now().astimezone(JST).date() != now.date():
                return
            if len(text) > 35000:
                raise AgentError("task_reminder_too_large", "今日のタスクの通知が長すぎます。")
            if not self.repository.reserve_reminder(key, self.clock.timestamp(), empty=not tasks):
                return
            if not tasks:
                return
            try:
                ts = self.sender.send(self.config.channel_id, text)
            except UncertainWrite:
                raise
            except AgentError:
                self.repository.release_reminder(key)
                raise
            self.repository.finish_reminder(key, ts, self.clock.timestamp())
        except AgentError as error:
            self._report(key, error.code)
        except Exception:
            self._report(key, "task_reminder_failed")
        else:
            self._last_error = None

    def _report(self, key: str, code: str) -> None:
        self._retry_at = self.clock.monotonic() + 60
        if self._last_error != (key, code):
            logger.critical("タスクの定期通知を確認してください: %s (%s)", key, code)
            self._last_error = (key, code)
