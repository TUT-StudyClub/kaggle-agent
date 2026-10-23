from abc import ABC, abstractmethod
from typing import Literal

ReminderState = Literal["sending", "sent", "empty"]


class ReminderRepository(ABC):
    @abstractmethod
    def reminder_state(self, key: str) -> ReminderState | None: ...

    @abstractmethod
    def reserve_reminder(self, key: str, now: float, *, empty: bool = False) -> bool:
        """日別の実行権を原子的に確保する。送信前に必ず永続化する。"""
        ...

    @abstractmethod
    def finish_reminder(self, key: str, message_ts: str, now: float) -> None: ...

    @abstractmethod
    def release_reminder(self, key: str) -> None:
        """Slackが明示的に拒否した送信だけ、再試行を許可する。"""
        ...
