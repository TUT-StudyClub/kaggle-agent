from abc import ABC, abstractmethod
from datetime import date


class RetroDocuments(ABC):
    @abstractmethod
    def members(self, members: list[dict]) -> tuple[str, ...]: ...

    @abstractmethod
    def create(self, day: date, user_ids: tuple[str, ...]) -> tuple[str, str]: ...

    @abstractmethod
    def ready(self, page_id: str, day: date, user_ids: tuple[str, ...]) -> bool: ...
