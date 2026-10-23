import re
from uuid import UUID

from pydantic import ValidationError

from shared.errors import AgentError
from shared.infrastructure.http import JsonHttp

from ...domain.models import CalendarMember
from ...domain.repositories import MemberDirectory


def property_text(props: dict, name: str) -> str:
    prop = props.get(name, {})
    return "".join(
        part.get("plain_text", part.get("text", {}).get("content", ""))
        for part in prop.get(prop.get("type", ""), [])
    ).strip()


class NotionMemberDirectory(MemberDirectory):
    """設定済みMember DBの対応列だけを参照する。行・スキーマは変更しない。"""

    def __init__(self, http: JsonHttp, data_sources: dict[str, str]):
        self.http = http
        self.data_sources = {alias: str(UUID(value)) for alias, value in data_sources.items()}

    @property
    def calendar_aliases(self) -> frozenset[str]:
        return frozenset(self.data_sources)

    def members(self, calendar_alias: str) -> list[CalendarMember]:
        if calendar_alias not in self.data_sources:
            raise AgentError(
                "unknown_calendar", "Member DBを設定したカレンダーを指定してください。"
            )
        path = f"data_sources/{self.data_sources[calendar_alias]}"
        schema = self.http.request("GET", path).get("properties", {})
        expected = {"Name": "title", "Slack User ID": "rich_text", "Calendar Label ID": "rich_text"}
        if "Calendar Emails" in schema:
            expected["Calendar Emails"] = "rich_text"
        if any(schema.get(name, {}).get("type") != kind for name, kind in expected.items()):
            raise AgentError(
                "member_schema",
                "Member DBのName・Slack User ID・Calendar Label IDの列と種類を確認してください。",
            )
        result = []
        cursor = None
        for _ in range(10):
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            raw = self.http.request("POST", f"{path}/query", read=True, json=payload)
            for row in raw["results"]:
                if row.get("archived") or row.get("in_trash") or row.get("is_archived"):
                    continue
                props = row["properties"]

                try:
                    result.append(
                        CalendarMember(
                            name=property_text(props, "Name"),
                            slack_name=property_text(props, "Slack's name"),
                            slack_user_id=property_text(props, "Slack User ID"),
                            label_id=property_text(props, "Calendar Label ID"),
                            url=row.get("url", ""),
                            calendar_emails=frozenset(
                                filter(
                                    None,
                                    re.split(r"[,;\s]+", property_text(props, "Calendar Emails")),
                                )
                            ),
                        )
                    )
                except ValidationError:
                    raise AgentError(
                        "member_mapping_invalid",
                        "Member DBの氏名・SlackユーザーID・ラベルIDに不備があります。"
                        "対応を確認してください。",
                    ) from None
            if not raw.get("has_more"):
                return result
            next_cursor = raw.get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                raise AgentError("member_incomplete", "Member DBの一覧を最後まで取得できません。")
            cursor = next_cursor
        raise AgentError("too_large", "Member DBの取得上限に達しました。対応表を確認してください。")
