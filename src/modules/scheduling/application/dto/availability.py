from pydantic import Field

from shared.domain.value_object import ValueObject

from ...domain.models import TimeRange


class AvailabilityMember(ValueObject):
    name: str
    slack_user_id: str


class AvailabilityResult(ValueObject):
    period: TimeRange
    minutes: int = Field(ge=1, le=1440)
    scope: str
    members: list[AvailabilityMember]
    free: list[TimeRange]
    daily_start_minute: int
    daily_end_minute: int
