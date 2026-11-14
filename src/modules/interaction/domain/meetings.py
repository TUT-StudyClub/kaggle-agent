from abc import ABC, abstractmethod
from typing import Literal

from shared.domain.value_object import ValueObject


class MeetingReceipt(ValueObject):
    key: str
    plan_digest: str
    phase: Literal[
        "creating_document",
        "document_created",
        "creating_event",
        "event_created",
        "posting",
        "complete",
    ] = "creating_event"
    event: dict[str, object] | None = None
    document_id: str | None = None
    document_url: str | None = None
    message_ts: str | None = None


class MeetingRepository(ABC):
    @abstractmethod
    def reserve_meeting(self, receipt: MeetingReceipt) -> bool: ...

    @abstractmethod
    def meeting(self, key: str) -> MeetingReceipt | None: ...

    @abstractmethod
    def save_meeting(self, receipt: MeetingReceipt) -> None: ...
