import json
import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, SecretStr

from modules.interaction.public import AgentPolicy


class Settings(AgentPolicy):
    database: Path = Path("var/agent.sqlite3")
    google_token_file: Path = Path("var/google-token.json")
    google_client_file: Path = Path("var/google-client-credentials.json")
    codex_timeout_seconds: int = Field(default=120, ge=10, le=300)
    slack_bot_token: SecretStr = SecretStr("")
    slack_app_token: SecretStr = SecretStr("")
    notion_token: SecretStr = SecretStr("")

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
            "channel_ids",
            "notion_pages",
            "notion_data_sources",
            "notion_parents",
            "calendars",
        }
        data = {}
        for field in cls.model_fields:
            key = field.upper()
            value = os.environ.get(key, values.get(key))
            if value is not None:
                data[field] = json.loads(value) if field in json_fields else value
        return cls.model_validate(data)
