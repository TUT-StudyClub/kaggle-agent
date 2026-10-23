import hashlib
import logging
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from modules.interaction.application.dto.reminders import DailyScheduleReminder
from modules.interaction.application.ports.reminders import ReminderSender
from modules.interaction.domain.reminders import ReminderRepository
from modules.scheduling.public import Event, SchedulingService, TimeRange
from shared.application.ports.clock import Clock
from shared.errors import AgentError, UncertainWrite

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")


def reminder_text(day: datetime, events: list[Event]) -> str:
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
        link = urlsplit(event.url)
        if link.scheme == "https" and link.hostname in {"www.google.com", "calendar.google.com"}:
            url = escape(event.url, quote=False).replace("|", "%7C")
            lines.append(f"• {when} {title} <{url}|予定を開く>")
        else:
            lines.append(f"• {when} {title}")
    return "\n".join(lines)


class RemindScheduleUseCase:
    def __init__(
        self,
        config: DailyScheduleReminder,
        team_id: str,
        scheduling: SchedulingService,
        repository: ReminderRepository,
        sender: ReminderSender,
        clock: Clock,
    ):
        self.config = config
        self.team_id = team_id
        self.scheduling = scheduling
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
            f"{self.team_id}/{self.config.channel_id}/"
            f"{self.config.calendar_alias}/{self.config.label_id}/{now.date()}"
        )
        key = hashlib.sha256(identity.encode()).hexdigest()
        try:
            state = self.repository.reminder_state(key)
            if state == "sending":
                raise UncertainWrite()
            if state is not None:
                return
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            events = self.scheduling.events_for_label(
                self.config.calendar_alias,
                self.config.label_id,
                TimeRange(start=start, end=start + timedelta(days=1)),
            )
            # 読み取りが日付をまたいだ場合、前日の予定を翌日に投稿しない。
            if self.clock.now().astimezone(JST).date() != now.date():
                return
            text = reminder_text(now, events) if events else ""
            if len(text) > 35000:
                raise AgentError("reminder_too_large", "チーム予定の通知が長すぎます。")
            if not self.repository.reserve_reminder(key, self.clock.timestamp(), empty=not events):
                return
            if not events:
                return
            try:
                message_ts = self.sender.send(self.config.channel_id, text)
            except UncertainWrite:
                raise
            except AgentError:
                self.repository.release_reminder(key)
                raise
            self.repository.finish_reminder(key, message_ts, self.clock.timestamp())
        except AgentError as error:
            self._report(key, error.code)
        except Exception:
            # 外部APIの本文・認証情報をログに出さず、Bot本体の処理も止めない。
            self._report(key, "reminder_failed")
        else:
            self._last_error = None

    def _report(self, key: str, code: str) -> None:
        self._retry_at = self.clock.monotonic() + 60
        if self._last_error != (key, code):
            logger.critical("チーム予定の定期通知を確認してください: %s (%s)", key, code)
            self._last_error = (key, code)
