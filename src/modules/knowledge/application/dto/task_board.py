from uuid import UUID

from pydantic import Field, field_validator

from shared.domain.value_object import ValueObject


class TaskBoardConfig(ValueObject):
    data_source_id: str
    member_data_source_id: str
    title_property: str = Field(default="Name", min_length=1)
    date_property: str = Field(default="Dead", min_length=1)
    assignee_property: str = Field(default="User", min_length=1)
    status_property: str = Field(default="Status", min_length=1)
    excluded_statuses: frozenset[str] = frozenset({"finish", "Done"})
    overdue_alert_property: str | None = Field(default=None, min_length=1)
    overdue_alert_value: str = Field(default="🚨 期限超過 🚨", min_length=1)

    @field_validator("data_source_id", "member_data_source_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        return str(UUID(value))
