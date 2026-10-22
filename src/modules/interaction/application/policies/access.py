from uuid import UUID

from pydantic import Field, field_validator, model_validator

from modules.interaction.domain.models import Source
from modules.scheduling.public import CalendarConfig
from shared.domain.value_object import ValueObject


class AgentPolicy(ValueObject):
    team_id: str = Field(min_length=1)
    user_ids: frozenset[str] = Field(min_length=1)
    channel_ids: frozenset[str] = Field(min_length=1)
    notion_pages: frozenset[str] = frozenset()
    notion_data_sources: frozenset[str] = frozenset()
    notion_competition_data_source_id: str | None = None
    notion_parents: dict[str, str] = {}
    calendars: dict[str, CalendarConfig] = {}
    retention_days: int = Field(default=7, ge=1, le=30)
    approval_minutes: int = Field(default=15, ge=1, le=60)
    max_ai_calls_per_day: int = Field(default=30, ge=1, le=1000)
    max_read_steps: int = Field(default=4, ge=1, le=8)

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
        return self

    def allows(self, source: Source) -> bool:
        return (
            source.team_id == self.team_id
            and source.user_id in self.user_ids
            and source.channel_id in self.channel_ids
        )
