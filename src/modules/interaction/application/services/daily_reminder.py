import hashlib
import logging
from abc import ABC, abstractmethod
from datetime import date, datetime
from zoneinfo import ZoneInfo

from modules.interaction.application.ports.reminders import ReminderSender
from modules.interaction.domain.reminders import ReminderRepository
from shared.application.ports.clock import Clock
from shared.errors import AgentError, UncertainWrite

JST = ZoneInfo("Asia/Tokyo")


class DailyReminder(ABC):
    """1日1回のチャンネル通知。日別の予約で重複送信を防ぎ、失敗は1分後に再試行する。"""

    logger: logging.Logger
    alert: str
    failure_code: str
    too_large: tuple[str, str]

    def __init__(
        self,
        channel_id: str,
        at: tuple[int, int],
        repository: ReminderRepository,
        sender: ReminderSender,
        clock: Clock,
    ):
        self.repository = repository
        self.sender = sender
        self.clock = clock
        self._channel_id = channel_id
        self._at = at
        self._last_error: tuple[str, str] | None = None
        self._retry_at = 0.0

    @abstractmethod
    def _identity(self, day: date) -> str:
        """通知先・対象・日付を一意に表す文字列。日別の予約キーの元になる。"""
        ...

    @abstractmethod
    def _text(self, now: datetime) -> str:
        """通知本文を組み立てる。通知する内容がない日は空文字を返す。"""
        ...

    def execute(self) -> None:
        if self.clock.monotonic() < self._retry_at:
            return
        now = self.clock.now().astimezone(JST)
        if (now.hour, now.minute) < self._at:
            return
        key = hashlib.sha256(self._identity(now.date()).encode()).hexdigest()
        try:
            state = self.repository.reminder_state(key)
            if state == "sending":
                raise UncertainWrite()
            if state is not None:
                return
            text = self._text(now)
            # 外部サービスの読取中に日付が変わっても前日分を投稿しない。
            if self.clock.now().astimezone(JST).date() != now.date():
                return
            if len(text) > 35000:
                raise AgentError(*self.too_large)
            if not self.repository.reserve_reminder(key, self.clock.timestamp(), empty=not text):
                return
            if not text:
                return
            try:
                message_ts = self.sender.send(self._channel_id, text)
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
            self._report(key, self.failure_code)
        else:
            self._last_error = None

    def _report(self, key: str, code: str) -> None:
        self._retry_at = self.clock.monotonic() + 60
        if self._last_error != (key, code):
            self.logger.critical("%s: %s (%s)", self.alert, key, code)
            self._last_error = (key, code)
