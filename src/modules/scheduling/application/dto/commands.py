from typing import Literal

from pydantic import Field

from shared.domain.value_object import ValueObject

from ...domain.models import EventDraft, TimeRange


class ListCalendars(ValueObject):
    kind: Literal["calendar.list"] = "calendar.list"


class ListEvents(ValueObject):
    kind: Literal["calendar.events"] = "calendar.events"
    calendar_alias: str
    period: TimeRange
    label_id: str | None = Field(default=None, min_length=1)
    slack_user_id: str | None = Field(default=None, pattern=r"^[UW][A-Z0-9]+$")


class FindFree(ValueObject):
    kind: Literal["calendar.free"] = "calendar.free"
    calendar_aliases: list[str] = Field(min_length=1, max_length=20)
    period: TimeRange
    minutes: int = Field(ge=1, le=1440)


class CreateEvent(ValueObject):
    kind: Literal["calendar.create"] = "calendar.create"
    calendar_alias: str
    draft: EventDraft


class UpdateEvent(ValueObject):
    kind: Literal["calendar.update"] = "calendar.update"
    calendar_alias: str
    event_id: str
    draft: EventDraft
    expected_etag: str | None = None
