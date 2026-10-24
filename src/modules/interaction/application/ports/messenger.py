from abc import ABC, abstractmethod
from enum import StrEnum

from modules.interaction.domain.models import Source


class Activity(StrEnum):
    IDLE = ""
    READING = "会話を確認しています…"
    THINKING = "考えています…"
    NOTION = "Notionを確認しています…"
    CALENDAR = "カレンダーを確認しています…"
    PREPARING = "操作内容を確認しています…"
    WRITING = "承認された操作を実行しています…"
    SENDING = "回答を送信しています…"


class Messenger(ABC):
    @abstractmethod
    def status(self, source: Source, activity: Activity) -> None: ...

    def deliver(self, notification_id: str, source: Source, text: str) -> None:
        self.send(source, text)

    @abstractmethod
    def send(self, source: Source, text: str) -> None: ...

    @abstractmethod
    def react(self, source: Source, message_ts: str, name: str) -> None: ...

    @abstractmethod
    def thread(self, source: Source) -> str: ...

    @abstractmethod
    def permalink(self, source: Source) -> str: ...
