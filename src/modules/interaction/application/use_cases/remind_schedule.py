import logging
from datetime import date, datetime, timedelta
from html import escape

from modules.interaction.application.dto.reminders import DailyScheduleReminder
from modules.interaction.application.ports.reminders import ReminderSender
from modules.interaction.application.services.daily_reminder import DailyReminder
from modules.interaction.domain.reminders import ReminderRepository
from modules.scheduling.public import Event, SchedulingService, TimeRange
from shared.application.ports.clock import Clock
from shared.domain.timezone import JST
from shared.errors import AgentError

logger = logging.getLogger(__name__)


def reminder_text(
    day: datetime,
    events: list[Event],
    participants: dict[str, frozenset[str]],
    channel_members: frozenset[str],
) -> str:
    def start(event: Event) -> datetime:
        value = datetime.fromisoformat(event.start)
        return value.replace(tzinfo=JST) if value.tzinfo is None else value.astimezone(JST)

    def boundary(value: str) -> str:
        local = datetime.fromisoformat(value).astimezone(JST)
        return local.strftime("%H:%M" if local.date() == day.date() else "%m/%d %H:%M")

    lines = [f"今日のチーム予定（{day.month}/{day.day}）"]
    for event in sorted(events, key=lambda e: (start(e), e.id)):
        when = (
            "終日" if len(event.start) == 10 else f"{boundary(event.start)}〜{boundary(event.end)}"
        )
        title = escape(" ".join(event.title.split()), quote=False)
        lines.append(f"• {when} {title}")
        users = participants[event.id] & channel_members
        mentions = (
            "<!channel>"
            if channel_members and users == channel_members
            else " ".join(f"<@{user_id}>" for user_id in sorted(users))
        )
        if mentions:
            lines.append(f"  参加者: {mentions}")
    return "\n".join(lines)


class RemindScheduleUseCase(DailyReminder):
    logger = logger
    alert = "チーム予定の定期通知を確認してください"
    failure_code = "reminder_failed"
    too_large = ("reminder_too_large", "チーム予定の通知が長すぎます。")

    def __init__(
        self,
        config: DailyScheduleReminder,
        team_id: str,
        scheduling: SchedulingService,
        repository: ReminderRepository,
        sender: ReminderSender,
        clock: Clock,
    ):
        super().__init__(config.channel_id, (config.hour, config.minute), repository, sender, clock)
        self.config = config
        self.team_id = team_id
        self.scheduling = scheduling

    def _identity(self, day: date) -> str:
        return (
            f"{self.team_id}/{self.config.channel_id}/"
            f"{self.config.calendar_alias}/{self.config.label_id}/{day}"
        )

    def _text(self, now: datetime) -> str:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        events = self.scheduling.events_for_label(
            self.config.calendar_alias,
            self.config.label_id,
            TimeRange(start=start, end=start + timedelta(days=1)),
        )
        if not events:
            return ""
        participants = self.scheduling.participant_users(self.config.calendar_alias, events)
        members = self.sender.human_members(self.config.channel_id)
        if not members:
            raise AgentError("channel_members_empty", "通知先のメンバーを確認できません。")
        return reminder_text(now, events, participants, members)
