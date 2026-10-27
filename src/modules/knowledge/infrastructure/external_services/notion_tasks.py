import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from shared.errors import AgentError
from shared.infrastructure.http import JsonHttp

from ...application.dto.task_board import TaskBoardConfig
from ...domain.tasks import BoardTask, TaskBoardRepository, TaskMember, TaskPerson
from .notion import notion_id

JST = ZoneInfo("Asia/Tokyo")


def text_value(prop: dict) -> str:
    return "".join(
        part.get("plain_text", part.get("text", {}).get("content", ""))
        for part in prop.get(prop.get("type", ""), [])
    ).strip()


class NotionTaskBoard(TaskBoardRepository):
    def __init__(self, http: JsonHttp, config: TaskBoardConfig):
        self.http = http
        self.config = config

    def _schema(self, source: str, expected: dict[str, str]) -> dict:
        schema = self.http.request("GET", f"data_sources/{source}").get("properties", {})
        if any(schema.get(name, {}).get("type") != kind for name, kind in expected.items()):
            raise AgentError(
                "task_board_schema", "タスク通知に使うNotionの列名と種類を確認してください。"
            )
        return schema

    def _pages(self, path: str, *, query: bool = False, payload: dict | None = None) -> list[dict]:
        rows = []
        cursor = None
        seen = set()
        for _ in range(10):
            args = {"page_size": 100, **(payload or {})}
            if cursor:
                args["start_cursor"] = cursor
            result = (
                self.http.request("POST", path, read=True, json=args)
                if query
                else self.http.request("GET", path, params=args)
            )
            rows.extend(result["results"])
            if not result.get("has_more"):
                return rows
            cursor = result.get("next_cursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        raise AgentError(
            "task_board_incomplete", "タスク通知のNotionデータを最後まで取得できません。"
        )

    def tasks_on(self, day: date) -> list[BoardTask]:
        c = self.config
        self._schema(
            c.data_source_id,
            {
                c.title_property: "title",
                c.date_property: "date",
                c.assignee_property: "people",
                c.status_property: "status",
            },
        )
        # 日付のみ・UTC・任意のオフセットを取りこぼさない候補を取得し、JSTで再判定する。
        conditions = [
            {
                "property": c.date_property,
                "date": {"on_or_after": (day - timedelta(days=1)).isoformat()},
            },
            {
                "property": c.date_property,
                "date": {"before": (day + timedelta(days=2)).isoformat()},
            },
            *(
                {"property": c.status_property, "status": {"does_not_equal": status}}
                for status in sorted(c.excluded_statuses)
            ),
        ]
        rows = self._pages(
            f"data_sources/{c.data_source_id}/query",
            query=True,
            payload={"filter": {"and": conditions}},
        )
        return self._tasks(rows, day=day)

    def overdue_tasks(self) -> list[BoardTask]:
        c = self.config
        if c.overdue_alert_property is None:
            return []
        self._schema(
            c.data_source_id,
            {
                c.title_property: "title",
                c.assignee_property: "people",
                c.status_property: "status",
                c.overdue_alert_property: "formula",
            },
        )
        conditions = [
            {
                "property": c.overdue_alert_property,
                "formula": {"string": {"equals": c.overdue_alert_value}},
            },
            *(
                {"property": c.status_property, "status": {"does_not_equal": status}}
                for status in sorted(c.excluded_statuses)
            ),
        ]
        rows = self._pages(
            f"data_sources/{c.data_source_id}/query",
            query=True,
            payload={"filter": {"and": conditions}},
        )
        return self._tasks(rows)

    def _tasks(self, rows: list[dict], *, day: date | None = None) -> list[BoardTask]:
        c = self.config
        tasks = []
        users: dict[str, TaskPerson] = {}
        for row in rows:
            if row.get("archived") or row.get("in_trash") or row.get("is_archived"):
                continue
            props = row["properties"]
            status = props[c.status_property].get("status") or {}
            if status.get("name") in c.excluded_statuses:
                continue
            start = ""
            if day is None:
                assert c.overdue_alert_property is not None
                formula = props[c.overdue_alert_property]["formula"]
                if formula.get("type") != "string":
                    raise AgentError(
                        "task_alert_type", "期限アラートの数式が文字列を返すか確認してください。"
                    )
                if formula.get("string") != c.overdue_alert_value:
                    continue
            else:
                value = props[c.date_property].get("date")
                if not value:
                    continue
                start = value["start"]
                if len(start) == 10:
                    local_day = date.fromisoformat(start)
                else:
                    local = datetime.fromisoformat(start)
                    if local.tzinfo is None:
                        timezone = value.get("time_zone")
                        if not timezone:
                            raise AgentError(
                                "task_date_timezone",
                                "タスクの日付のタイムゾーンを確認してください。",
                            )
                        local = local.replace(tzinfo=ZoneInfo(timezone))
                    local_day = local.astimezone(JST).date()
                if local_day != day:
                    continue
            prop = props[c.assignee_property]
            people = prop["people"]
            if len(people) >= 25 or prop.get("has_more"):
                items = self._pages(f"pages/{notion_id(row['id'])}/properties/{prop['id']}")
                people = [item["people"] for item in items]
            assignees = []
            for person in people:
                uid = notion_id(person["id"])
                if uid not in users:
                    raw = self.http.request("GET", f"users/{uid}")
                    if raw.get("type") != "person" or notion_id(raw["id"]) != uid:
                        raise AgentError(
                            "task_person_invalid", "Notionの担当者情報を確認できません。"
                        )
                    users[uid] = TaskPerson(
                        id=uid,
                        name=raw.get("name") or "",
                        email=raw.get("person", {}).get("email") or "",
                    )
                assignees.append(users[uid])
            tasks.append(
                BoardTask(
                    id=notion_id(row["id"]),
                    title=text_value(props[c.title_property]) or "（無題）",
                    url=row.get("url", ""),
                    start=start,
                    assignees=tuple(assignees),
                )
            )
        return tasks

    def members(self) -> list[TaskMember]:
        source = self.config.member_data_source_id
        schema = self._schema(source, {"Name": "title", "Slack User ID": "rich_text"})
        if "Calendar Emails" in schema and schema["Calendar Emails"].get("type") != "rich_text":
            raise AgentError(
                "task_board_schema", "Member DBのCalendar Emailsの種類を確認してください。"
            )
        result = []
        for row in self._pages(f"data_sources/{source}/query", query=True):
            if row.get("archived") or row.get("in_trash") or row.get("is_archived"):
                continue
            props = row["properties"]
            emails = text_value(props.get("Calendar Emails", {}))
            emails = re.sub(r"(?m)^\s*(?:[•・]\s*|[-*]\s+)", "", emails)
            result.append(
                TaskMember(
                    name=text_value(props["Name"]),
                    slack_user_id=text_value(props["Slack User ID"]),
                    emails=frozenset(e.casefold() for e in re.split(r"[,;\s]+", emails) if e),
                )
            )
        return result
