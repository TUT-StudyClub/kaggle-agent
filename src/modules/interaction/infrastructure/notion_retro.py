import time
from collections import Counter
from datetime import date
from uuid import UUID

from modules.interaction.application.ports.retro_documents import RetroDocuments
from shared.errors import AgentError
from shared.infrastructure.http import JsonHttp


class NotionRetroDocuments(RetroDocuments):
    def __init__(self, http: JsonHttp, data_source_id: str, template_id: str):
        self.http = http
        self.data_source_id = str(UUID(data_source_id))
        self.template_id = str(UUID(template_id))

    def members(self, members: list[dict]) -> tuple[str, ...]:
        schema = self.http.request("GET", f"data_sources/{self.data_source_id}")
        properties = schema.get("properties", {})
        expected = {"Name": "title", "Date": "date", "User": "people", "Type": "select"}
        if any(properties.get(name, {}).get("type") != kind for name, kind in expected.items()):
            raise AgentError("retro_schema", "Document DBのName・Date・User・Typeを確認してね。")
        self.http.request("GET", f"pages/{self.template_id}")
        ids = []
        if len(members) > 25:
            raise AgentError("retro_users_limit", "レトプラの参加者は25人以下にしてください。")
        for member in members:
            name = member["name"]
            matches = member.get("notion_user_ids", ())
            if len(matches) != 1:
                raise AgentError(
                    "retro_user_mapping", f"Member DBで{name}のNotion Userを1人に設定してね。"
                )
            try:
                ids.append(str(UUID(matches[0])))
            except TypeError, ValueError:
                raise AgentError("retro_user_mapping", "NotionユーザーのIDを確認してね。") from None
        if len(set(ids)) != len(ids):
            raise AgentError("retro_user_mapping", "Notionユーザーの対応が重複しているよ。")
        return tuple(ids)

    def create(self, day: date, user_ids: tuple[str, ...]) -> tuple[str, str]:
        raw = self.http.request(
            "POST",
            "pages",
            json={
                "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
                "properties": {
                    "Name": {
                        "title": [{"text": {"content": f"[Retro & Planning] {day:%Y/%m/%d}"}}]
                    },
                    "Type": {"select": {"name": "Retro & Planning"}},
                    "Date": {"date": {"start": day.isoformat()}},
                    "User": {"people": [{"id": user_id} for user_id in user_ids]},
                },
                "template": {
                    "type": "template_id",
                    "template_id": self.template_id,
                    "timezone": "Asia/Tokyo",
                },
            },
        )
        return str(UUID(raw["id"])), raw["url"]

    def _blocks(self, page_id: str) -> list[dict]:
        blocks = []
        cursor = None
        for _ in range(10):
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            result = self.http.request("GET", f"blocks/{page_id}/children", params=params)
            blocks.extend(result.get("results", []))
            if not result.get("has_more"):
                return blocks
            next_cursor = result.get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        raise AgentError("retro_template_incomplete", "テンプレートの本文を確認できないよ。")

    def ready(self, page_id: str, day: date, user_ids: tuple[str, ...]) -> bool:
        expected_blocks = Counter(block.get("type") for block in self._blocks(self.template_id))
        if not expected_blocks:
            return False
        for attempt in range(10):
            page = self.http.request("GET", f"pages/{page_id}")
            properties = page.get("properties", {})
            title = "".join(
                part.get("plain_text", "") for part in properties.get("Name", {}).get("title", [])
            )
            date_value = properties.get("Date", {}).get("date") or {}
            people = properties.get("User", {}).get("people", [])
            user_set = {str(UUID(person["id"])) for person in people}
            actual_blocks = Counter(block.get("type") for block in self._blocks(page_id))
            if (
                title == f"[Retro & Planning] {day:%Y/%m/%d}"
                and date_value.get("start") == day.isoformat()
                and user_set == set(user_ids)
                and (properties.get("Type", {}).get("select") or {}).get("name")
                == "Retro & Planning"
                and all(actual_blocks[kind] >= count for kind, count in expected_blocks.items())
            ):
                return True
            if attempt < 9:
                time.sleep(1)
        return False
