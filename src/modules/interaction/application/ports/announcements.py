from abc import ABC, abstractmethod


class AnnouncementSender(ABC):
    @abstractmethod
    def channels(self) -> list[dict[str, str]]: ...

    @abstractmethod
    def send(self, channel_id: str, text: str, key: str) -> str: ...

    @abstractmethod
    def find(self, channel_id: str, text: str, key: str) -> str | None: ...
