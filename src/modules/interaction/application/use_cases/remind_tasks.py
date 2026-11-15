import logging
from datetime import date, datetime
from html import escape

from modules.knowledge.public import TaskBoardService, TaskNotification
from shared.application.ports.clock import Clock
from shared.errors import AgentError

from ...domain.reminders import ReminderRepository
from ..dto.reminders import DailyTaskReminder
from ..ports.reminders import ReminderSender
from ..services.daily_reminder import DailyReminder

logger = logging.getLogger(__name__)


def task_reminder_text(
    day: datetime, tasks: list[TaskNotification], *, heading: str = "今日のタスク"
) -> str:
    lines = [f"{heading}（{day.month}/{day.day}）"]
    for task in sorted(tasks, key=lambda t: (t.title, t.id)):
        title = escape(" ".join(task.title.split()), quote=False)
        lines.append(f"• {title}")
        mentions = " ".join(f"<@{uid}>" for uid in sorted(task.slack_user_ids))
        lines.append(f"  担当者: {mentions or '未設定'}")
    return "\n".join(lines)


class RemindTasksUseCase(DailyReminder):
    logger = logger
    alert = "タスクの定期通知を確認してください"
    failure_code = "task_reminder_failed"
    too_large = ("task_reminder_too_large", "タスクの通知が長すぎます。")

    def __init__(
        self,
        config: DailyTaskReminder,
        team_id: str,
        board: TaskBoardService,
        repository: ReminderRepository,
        sender: ReminderSender,
        clock: Clock,
    ):
        super().__init__(config.channel_id, (config.hour, config.minute), repository, sender, clock)
        self.config = config
        self.team_id = team_id
        self.board = board

    def _identity(self, day: date) -> str:
        return f"tasks/{self.team_id}/{self.config.channel_id}/{self.config.data_source_id}/{day}"

    def _text(self, now: datetime) -> str:
        tasks = self.board.tasks_on(now.date())
        overdue = self.board.overdue_tasks() if self.config.overdue_alert_property else []
        all_tasks = tasks + overdue
        if not all_tasks:
            return ""
        members = self.sender.human_members(self.config.channel_id)
        assigned = set().union(*(t.slack_user_ids for t in all_tasks))
        if not members or not assigned <= members:
            raise AgentError(
                "task_channel_member_missing",
                "タスク担当者が通知先チャンネルにいるか確認してください。",
            )
        sections = [task_reminder_text(now, tasks)] if tasks else []
        if overdue:
            sections.append(task_reminder_text(now, overdue, heading="期限超過のタスク"))
        return "\n\n".join(sections)
