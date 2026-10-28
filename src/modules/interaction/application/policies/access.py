from uuid import UUID

from pydantic import Field, field_validator, model_validator

from modules.interaction.domain.models import Source
from modules.scheduling.public import CalendarConfig
from shared.domain.value_object import ValueObject


class AgentPolicy(ValueObject):
    team_id: str = Field(min_length=1)
    user_ids: frozenset[str] = Field(min_length=1)
    dm_user_ids: frozenset[str] | None = None
    kaggle_owner_user_id: str | None = Field(default=None, pattern=r"^[UW][A-Z0-9]+$")
    channel_ids: frozenset[str] = Field(min_length=1)
    notion_pages: frozenset[str] = frozenset()
    notion_data_sources: frozenset[str] = frozenset()
    notion_competition_data_source_id: str | None = None
    notion_parents: dict[str, str] = {}
    calendars: dict[str, CalendarConfig] = {}
    retention_days: int = Field(default=7, ge=1, le=30)

    @field_validator("notion_competition_data_source_id")
    @classmethod
    def normalize_competition_source(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value else None

    @property
    def allowed_notion_data_sources(self) -> frozenset[str]:
        competition = self.notion_competition_data_source_id
        return self.notion_data_sources | (frozenset({competition}) if competition else frozenset())

    @model_validator(mode="after")
    def no_empty_ids(self) -> AgentPolicy:
        if any(not v for v in (*self.user_ids, *self.channel_ids)):
            raise ValueError("許可IDに空文字は使えません")
        if self.dm_user_ids is not None and not self.dm_user_ids <= self.user_ids:
            raise ValueError("DMの許可ユーザーはUSER_IDSに含めてください")
        if self.kaggle_owner_user_id is not None and self.kaggle_owner_user_id not in self.user_ids:
            raise ValueError("Kaggleの管理者はUSER_IDSに含めてください")
        return self

    def allows(self, source: Source) -> bool:
        return (
            source.team_id == self.team_id
            and source.user_id in self.user_ids
            and source.channel_id in self.channel_ids
            and (
                not source.channel_id.startswith("D")
                or self.dm_user_ids is None
                or source.user_id in self.dm_user_ids
            )
        )
