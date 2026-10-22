from abc import ABC, abstractmethod

from .models import Calendar, CalendarMember, Event, EventDraft, TimeRange


class MemberDirectory(ABC):
    @property
    @abstractmethod
    def calendar_aliases(self) -> frozenset[str]: ...

    @abstractmethod
    def members(self, calendar_alias: str) -> list[CalendarMember]: ...


class CalendarRepository(ABC):
    @abstractmethod
    def calendars(self) -> list[Calendar]: ...

    @abstractmethod
    def events(self, alias: str, period: TimeRange) -> list[Event]: ...

    @abstractmethod
    def busy(self, aliases: list[str], period: TimeRange) -> list[TimeRange]: ...

    @abstractmethod
    def event(self, alias: str, event_id: str) -> Event: ...

    @abstractmethod
    def create(self, alias: str, draft: EventDraft, key: str) -> Event: ...

    @abstractmethod
    def update(self, alias: str, event: Event, draft: EventDraft, key: str) -> Event: ...

    @abstractmethod
    def find_applied(self, alias: str, event_id: str | None, key: str) -> Event | None: ...
