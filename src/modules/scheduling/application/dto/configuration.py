import re
from uuid import UUID

from pydantic import Field, model_validator

from shared.domain.value_object import ValueObject

from ...domain.models import EventLabel


class CalendarConfig(ValueObject):
    id: str = Field(min_length=1)
    writable: bool = False
    label_users: dict[str, str] = Field(default_factory=dict)
    member_data_source_id: str | None = None
    participant_label_ids: frozenset[str] = frozenset()
    creation_label: EventLabel | None = None

    @model_validator(mode="after")
    def valid_label_users(self) -> CalendarConfig:
        if self.creation_label is not None and (
            not self.creation_label.id.strip()
            or not re.fullmatch(r"#[0-9a-fA-F]{6}", self.creation_label.background_color)
        ):
            raise ValueError("作成時のラベルには既存ラベルIDと6桁の16進数の色を指定してください")
        if any(not label_id.strip() for label_id in self.participant_label_ids):
            raise ValueError("参加者で判定する予定のラベルIDに空文字は使えません")
        if self.participant_label_ids and self.member_data_source_id is None:
            raise ValueError("共通予定の参加者判定にはMember DBを設定してください")
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
