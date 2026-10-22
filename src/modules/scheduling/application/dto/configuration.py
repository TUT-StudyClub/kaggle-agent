import re
from uuid import UUID

from pydantic import Field, model_validator

from shared.domain.value_object import ValueObject


class CalendarConfig(ValueObject):
    id: str = Field(min_length=1)
    writable: bool = False
    label_users: dict[str, str] = Field(default_factory=dict)
    member_data_source_id: str | None = None

    @model_validator(mode="after")
    def valid_label_users(self) -> CalendarConfig:
        if self.member_data_source_id is not None:
            UUID(self.member_data_source_id)
            if self.label_users:
                raise ValueError("Member DBと固定のlabel_usersは同時に指定できません")
        if any(
            not label_id.strip() or not re.fullmatch(r"[UW][A-Z0-9]+", user_id)
            for label_id, user_id in self.label_users.items()
        ):
            raise ValueError("ラベルIDとSlackユーザーID（U/Wで始まる英数字）が必要です")
        return self
