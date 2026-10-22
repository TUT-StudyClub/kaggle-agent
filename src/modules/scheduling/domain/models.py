from datetime import datetime, timedelta

from pydantic import AwareDatetime, Field, model_validator

from shared.domain.value_object import ValueObject


class TimeRange(ValueObject):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> TimeRange:
        if self.end <= self.start:
            raise ValueError("終了日時は開始日時より後にしてください")
        if self.end - self.start > timedelta(days=93):
            raise ValueError("期間は93日以内にしてください")
        return self


class EventLabel(ValueObject):
    id: str
    name: str = ""
    background_color: str = ""


class CalendarMember(ValueObject):
    name: str = Field(min_length=1)
    slack_name: str = ""
    slack_user_id: str = Field(pattern=r"^[UW][A-Z0-9]+$")
    label_id: str = Field(min_length=1)
    url: str = ""


class Calendar(ValueObject):
    alias: str
    id: str
    name: str
    role: str
    owned: bool
    labels: list[EventLabel] = Field(default_factory=list)


class Event(ValueObject):
    id: str
    calendar_alias: str
    title: str
    start: str
    end: str
    url: str
    etag: str
    editable: bool = False
    location: str = ""
    label_id: str | None = None
    label: EventLabel | None = None
    color_id: str | None = None


class EventDraft(ValueObject):
    title: str = Field(min_length=1, max_length=200)
    period: TimeRange


def free_slots(window: TimeRange, busy: list[TimeRange], minutes: int) -> list[TimeRange]:
    """重複Busyを統合し、指定長以上の連続した空き区間を返す。"""
    cursor: datetime = window.start
    result = []
    for item in sorted(busy, key=lambda value: value.start):
        start, end = max(item.start, window.start), min(item.end, window.end)
        if end <= cursor or start >= window.end:
            continue
        if start - cursor >= timedelta(minutes=minutes):
            result.append(TimeRange(start=cursor, end=start))
        cursor = max(cursor, end)
    if window.end - cursor >= timedelta(minutes=minutes):
        result.append(TimeRange(start=cursor, end=window.end))
    return result
