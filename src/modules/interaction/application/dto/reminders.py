from pydantic import Field

from shared.domain.value_object import ValueObject


class DailyScheduleReminder(ValueObject):
    calendar_alias: str = Field(min_length=1)
    label_id: str = Field(min_length=1)
    channel_id: str = Field(pattern=r"^[CG][A-Z0-9]+$")
    hour: int = Field(default=10, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
