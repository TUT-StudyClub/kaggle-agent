import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

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
    slack_user_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    daily_start_minute: int = Field(default=0, ge=0, le=1439)
    daily_end_minute: int = Field(default=1440, ge=1, le=1440)

    @model_validator(mode="after")
    def valid_selection(self) -> FindFree:
        if self.slack_user_ids is not None and (
            len(set(self.slack_user_ids)) != len(self.slack_user_ids)
            or any(not re.fullmatch(r"[UW][A-Z0-9]+", user) for user in self.slack_user_ids)
        ):
            raise ValueError("対象者には重複しないSlackユーザーIDを指定してください")
        if len(set(self.calendar_aliases)) != len(self.calendar_aliases):
            raise ValueError("同じカレンダーを重複して指定できません")
        if self.daily_start_minute >= self.daily_end_minute:
            raise ValueError("日ごとの検索終了時刻は開始時刻より後にしてください")
        return self


class CreateEvent(ValueObject):
    kind: Literal["calendar.create"] = "calendar.create"
    calendar_alias: str
    draft: EventDraft
    guest_user_ids: list[str] = Field(default_factory=list, max_length=100)
    guest_names: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("guest_user_ids")
    @classmethod
    def valid_guests(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(
            not re.fullmatch(r"[UW][A-Z0-9]+", value) for value in values
        ):
            raise ValueError("招待する人には重複しないSlackユーザーIDを指定してください")
        return values


class UpdateEvent(ValueObject):
    kind: Literal["calendar.update"] = "calendar.update"
    calendar_alias: str
    event_id: str
    draft: EventDraft
    expected_etag: str | None = None

    @model_validator(mode="after")
    def no_guest_changes(self) -> UpdateEvent:
        if self.draft.guest_emails:
            raise ValueError("既存の予定のゲストは変更できません")
        if self.draft.label is not None:
            raise ValueError("既存の予定のラベルは保持してください")
        return self
