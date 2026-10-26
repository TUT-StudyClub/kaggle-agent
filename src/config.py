import json
import os
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values
from pydantic import Field, SecretStr, model_validator

from modules.interaction.public import AgentPolicy, DailyScheduleReminder, DailyTaskReminder


class Settings(AgentPolicy):
    database: Path = Path("var/agent.sqlite3")
    google_token_file: Path = Path("var/google-token.json")
    google_client_file: Path = Path("var/google-client-credentials.json")
    codex_timeout_seconds: int = Field(default=120, ge=10, le=300)
    slack_bot_token: SecretStr = SecretStr("")
    slack_app_token: SecretStr = SecretStr("")
    notion_token: SecretStr = SecretStr("")
    kaggle_api_token: SecretStr = SecretStr("")
    kaggle_config_dir: Path = Path.home() / ".kaggle"
    kaggle_workspace: Path = Path("var/kaggle")
    daily_schedule_reminder: DailyScheduleReminder | None = None
    daily_task_reminder: DailyTaskReminder | None = None

    @model_validator(mode="after")
    def valid_reminder(self) -> Settings:
        reminder = self.daily_schedule_reminder
        if reminder is not None:
            if reminder.calendar_alias not in self.calendars:
                raise ValueError("定期通知には許可したカレンダーの別名を指定してください")
            if reminder.channel_id not in self.channel_ids:
                raise ValueError("定期通知には許可したSlackチャンネルIDを指定してください")
        task_reminder = self.daily_task_reminder
        if task_reminder is not None:
            if task_reminder.channel_id not in self.channel_ids:
                raise ValueError("タスク通知には許可したSlackチャンネルIDを指定してください")
            allowed = {str(UUID(value)) for value in self.allowed_notion_data_sources}
            if not {task_reminder.data_source_id, task_reminder.member_data_source_id} <= allowed:
                raise ValueError("タスク通知には許可したNotionデータソースIDを指定してください")
        return self

    def agent_policy(self) -> AgentPolicy:
        return AgentPolicy.model_validate(
            {name: getattr(self, name) for name in AgentPolicy.model_fields}
        )

    @classmethod
    def load(cls, path: Path = Path(".env")) -> Settings:
        # 環境変数を優先し、認証情報の $ 等を変数展開しない。
        values = dotenv_values(path, interpolate=False)
        json_fields = {
            "user_ids",
            "dm_user_ids",
            "channel_ids",
            "notion_pages",
            "notion_data_sources",
            "notion_parents",
            "calendars",
            "daily_schedule_reminder",
            "daily_task_reminder",
        }
        data = {}
        for field in cls.model_fields:
            key = field.upper()
            value = os.environ.get(key, values.get(key))
            if field == "kaggle_owner_user_id" and value == "":
                value = None
            if value is not None:
                data[field] = json.loads(value) if field in json_fields else value
        return cls.model_validate(data)
