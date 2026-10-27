import json
import sqlite3
from contextlib import ExitStack
from datetime import date, datetime
from unittest.mock import Mock, create_autospec

import httpx
import pytest
from pydantic import ValidationError
from slack_sdk import WebClient

import composition
from config import Settings
from modules.interaction.application.use_cases.remind_tasks import RemindTasksUseCase
from modules.interaction.infrastructure.messaging.slack_reminders import SlackReminderSender
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.interaction.public import DailyTaskReminder
from modules.knowledge.composition import build_task_board
from shared.application.ports.clock import Clock
from shared.errors import AgentError, UncertainWrite
from shared.infrastructure.http import JsonHttp

TASKS = "00000000-0000-0000-0000-000000000010"
MEMBERS = "00000000-0000-0000-0000-000000000020"
PERSON = "00000000-0000-0000-0000-000000000030"
OTHER_PERSON = "00000000-0000-0000-0000-000000000031"


class TaskClock(Clock):
    value = "2026-10-26T00:00:00+00:00"

    def now(self):
        return datetime.fromisoformat(self.value)

    def monotonic(self):
        return self.timestamp()


def prop(kind, text):
    return {"type": kind, kind: [{"plain_text": text}]}


def task(index=1, start="2026-10-26", status="ToDo", people=None, alert="", **extra):
    return {
        "id": f"00000000-0000-0000-0000-{index:012}",
        "url": "https://www.notion.so/task",
        "properties": {
            "Name": prop("title", f"タスク{index}"),
            "Dead": {"type": "date", "date": {"start": start, "end": "2026-10-30"}},
            "Status": {"type": "status", "status": {"name": status}},
            "期限アラート": {"type": "formula", "formula": {"type": "string", "string": alert}},
            "User": {
                "id": "assignee",
                "type": "people",
                "people": ([{"id": PERSON}] if people is None else people),
            },
        },
        **extra,
    }


@pytest.fixture
def board_runtime(tmp_path):
    config = DailyTaskReminder(data_source_id=TASKS, member_data_source_id=MEMBERS, channel_id="C1")
    clock = TaskClock()
    slack = create_autospec(WebClient, instance=True)
    slack.chat_postMessage.return_value = {"ok": True, "ts": "123.456"}
    slack.conversations_members.return_value = {"members": ["U1", "U2", "UBOT"]}
    slack.users_info.side_effect = lambda *, user: {"user": {"id": user, "is_bot": user == "UBOT"}}
    rows = [task()]
    member_rows = [
        {
            "properties": {
                "Name": prop("title", "山田 祐平"),
                "Slack User ID": prop("rich_text", "U1"),
                "Calendar Emails": prop("rich_text", "other@example.com"),
            }
        }
    ]
    users = {
        PERSON: {
            "id": PERSON,
            "type": "person",
            "name": "山田祐平",
            "person": {"email": "user@example.com"},
        },
        OTHER_PERSON: {
            "id": OTHER_PERSON,
            "type": "person",
            "name": "別の表示名",
            "person": {"email": "alias@example.com"},
        },
    }
    requests = []
    failures = {}

    def respond(request):
        requests.append(request)
        path = request.url.path
        if path in failures:
            return httpx.Response(failures[path], json={})
        if path.startswith("/users/"):
            return httpx.Response(200, json=users[path.rsplit("/", 1)[1]])
        if path.startswith("/pages/"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"type": "people", "people": {"id": PERSON}},
                        {"type": "people", "people": {"id": OTHER_PERSON}},
                    ],
                    "has_more": False,
                },
            )
        if request.method == "GET":
            schema = (
                {
                    "Name": "title",
                    "Dead": "date",
                    "Status": "status",
                    "User": "people",
                    "期限アラート": "formula",
                }
                if TASKS in path
                else {"Name": "title", "Slack User ID": "rich_text", "Calendar Emails": "rich_text"}
            )
            return httpx.Response(
                200, json={"properties": {k: {"type": v} for k, v in schema.items()}}
            )
        assert request.method == "POST" and path.endswith("/query")
        values = rows if TASKS in path else member_rows
        # 2ページに分割し、完了まで取得されることを検証する。
        cursor = json.loads(request.content).get("start_cursor")
        return httpx.Response(
            200,
            json={
                "results": values[1:] if cursor else values[:1],
                "has_more": not cursor and len(values) > 1,
                "next_cursor": "next" if not cursor and len(values) > 1 else None,
            },
        )

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(respond)
    ) as client:
        board = build_task_board(JsonHttp(client, sleep=lambda _: None), config)
        path = tmp_path / "agent.sqlite3"

        def restart():
            return RemindTasksUseCase(
                config,
                "T1",
                board,
                SQLiteJobRepository(path),
                SlackReminderSender(slack, frozenset({"C1"})),
                clock,
            )

        yield restart(), restart, clock, slack, rows, member_rows, users, requests, path, failures


def test_jst_start_date_completed_tasks_excluded_and_restart_is_idempotent(board_runtime):
    usecase, restart, clock, slack, rows, _, _, requests, path, _ = board_runtime
    rows[:] = [
        task(1),
        task(2, status="Done"),
        task(3, status="finish"),
        task(4, start="2026-10-25"),  # 期間に今日を含んでも開始日が昨日なら除外。
        task(5, start="2026-10-25T15:00:00+00:00"),
        task(6, start="2026-10-26T15:00:00+00:00"),
        task(7, archived=True),
        task(8, people=[]),
    ]
    clock.value = "2026-10-25T23:59:59+00:00"
    usecase.execute()
    assert not requests
    clock.value = "2026-10-26T00:00:00+00:00"
    usecase.execute()
    sent = slack.chat_postMessage.call_args.kwargs
    assert sent["channel"] == "C1" and "thread_ts" not in sent
    assert sent["text"].startswith("今日のタスク（10/26）")
    assert "タスクを開く" not in sent["text"] and "https://www.notion.so/task" not in sent["text"]
    assert all(f"タスク{i}" in sent["text"] for i in (1, 5, 8))
    assert all(f"タスク{i}" not in sent["text"] for i in (2, 3, 4, 6, 7))
    assert "<@U1>" in sent["text"] and "担当者: 未設定" in sent["text"]
    assert "<!channel>" not in sent["text"]
    query = next(
        json.loads(r.content) for r in requests if r.method == "POST" and TASKS in r.url.path
    )
    assert {"property": "Status", "status": {"does_not_equal": "finish"}} in query["filter"]["and"]
    assert {"property": "Status", "status": {"does_not_equal": "Done"}} in query["filter"]["and"]
    assert sum(r.url.path == f"/users/{PERSON}" for r in requests) == 1
    reads = len(requests)
    restart().execute()
    assert len(requests) == reads
    slack.chat_postMessage.assert_called_once()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM schedule_reminders").fetchall() == [("sent",)]


def test_email_alias_and_multiple_assignees_are_matched_without_fuzzy_names(board_runtime):
    usecase, _, _, slack, rows, members, _, _, _, _ = board_runtime
    rows[:] = [task(people=[{"id": PERSON}, {"id": OTHER_PERSON}, {"id": PERSON}])]
    members.append(
        {
            "properties": {
                "Name": prop("title", "違う名前"),
                "Slack User ID": prop("rich_text", "U2"),
                "Calendar Emails": prop(
                    "rich_text", "  • first@example.com\n  • ALIAS@EXAMPLE.COM"
                ),
            }
        }
    )
    usecase.execute()
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert "担当者: <@U1> <@U2>" in text
    assert text.count("<@U1>") == 1 and "<!channel>" not in text


@pytest.mark.parametrize(
    "failure", ["unknown", "ambiguous", "conflict", "outside_channel", "notion_permission"]
)
def test_unverified_identity_never_reserves_or_sends(board_runtime, failure, caplog):
    usecase, _, _, slack, _, members, users, _, path, failures = board_runtime
    if failure == "unknown":
        users[PERSON]["name"] = "山田"  # 部分一致では送らない。
    elif failure == "outside_channel":
        members[0]["properties"]["Slack User ID"] = prop("rich_text", "U9")
    elif failure == "notion_permission":
        failures[f"/users/{PERSON}"] = 403
    else:
        members.append(
            {
                "properties": {
                    "Name": prop("title", "山田祐平" if failure == "ambiguous" else "別人"),
                    "Slack User ID": prop("rich_text", "U2"),
                    "Calendar Emails": prop("rich_text", "user@example.com"),
                }
            }
        )
    usecase.execute()
    slack.chat_postMessage.assert_not_called()
    assert caplog.records
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT * FROM schedule_reminders").fetchall()


def test_truncated_people_are_loaded_from_property_endpoint(board_runtime):
    usecase, _, _, slack, rows, members, _, requests, _, _ = board_runtime
    rows[:] = [task(people=[{"id": PERSON}] * 25)]
    members.append(
        {
            "properties": {
                "Name": prop("title", "別名"),
                "Slack User ID": prop("rich_text", "U2"),
                "Calendar Emails": prop("rich_text", "alias@example.com"),
            }
        }
    )
    usecase.execute()
    assert any("/properties/assignee" in r.url.path for r in requests)
    assert "<@U2>" in slack.chat_postMessage.call_args.kwargs["text"]


def test_read_failure_retries_and_uncertain_send_does_not_repeat(board_runtime):
    usecase, restart, clock, slack, _, _, _, _, path, failures = board_runtime
    failures[f"/data_sources/{TASKS}/query"] = 503
    usecase.execute()
    slack.chat_postMessage.assert_not_called()
    failures.clear()
    usecase.execute()
    slack.chat_postMessage.assert_not_called()
    clock.value = "2026-10-26T10:01:00+09:00"
    slack.chat_postMessage.side_effect = OSError("private-error")
    usecase.execute()
    restart().execute()
    slack.chat_postMessage.assert_called_once()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM schedule_reminders").fetchall() == [("sending",)]


def test_empty_day_is_remembered_and_next_day_is_independent(board_runtime):
    usecase, restart, clock, slack, rows, _, _, requests, _, _ = board_runtime
    rows.clear()
    usecase.execute()
    count = len(requests)
    restart().execute()
    assert len(requests) == count
    rows.append(task(start="2026-10-27"))
    clock.value = "2026-10-27T10:00:00+09:00"
    restart().execute()
    slack.chat_postMessage.assert_called_once()


def test_task_text_escapes_mentions_and_rejects_untrusted_links(board_runtime):
    usecase, _, _, slack, rows, _, _, _, _, _ = board_runtime
    rows[0]["properties"]["Name"] = prop("title", "<!channel> <@U9> & <https://evil.invalid|link>")
    rows[0]["url"] = "https://evil.invalid/"
    usecase.execute()
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert "<!channel>" not in text and "<@U9>" not in text
    assert "&lt;!channel&gt;" in text and "<https://evil.invalid" not in text


def test_crossing_midnight_does_not_send_yesterdays_tasks(board_runtime):
    usecase, _, clock, slack, _, _, _, _, _, _ = board_runtime
    clock.value = "2026-10-26T23:59:59+09:00"
    original = usecase.board.tasks_on

    def cross(day):
        result = original(day)
        clock.value = "2026-10-27T00:00:00+09:00"
        return result

    usecase.board.tasks_on = cross
    usecase.execute()
    slack.chat_postMessage.assert_not_called()


def test_incomplete_pagination_never_becomes_a_partial_notification():
    config = DailyTaskReminder(data_source_id=TASKS, member_data_source_id=MEMBERS, channel_id="C1")

    def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "properties": {
                        key: {"type": kind}
                        for key, kind in {
                            "Name": "title",
                            "Dead": "date",
                            "User": "people",
                            "Status": "status",
                        }.items()
                    }
                },
            )
        return httpx.Response(200, json={"results": [task()], "has_more": True})

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(AgentError, match="最後まで"):
            build_task_board(JsonHttp(client), config).tasks_on(date(2026, 10, 26))


def test_explicit_rejection_retries_but_uncertain_send_does_not(board_runtime):
    usecase, restart, clock, _, _, _, _, _, _, _ = board_runtime
    sender = Mock()
    sender.human_members.return_value = {"U1"}
    sender.send.side_effect = AgentError("rejected", "")
    usecase.sender = sender
    usecase.execute()
    clock.value = "2026-10-26T10:01:00+09:00"
    sender.send.side_effect = UncertainWrite()
    usecase.execute()
    restart().execute()
    assert sender.send.call_count == 2


def test_task_reminder_json_and_allowlist_validation(tmp_path):
    payload = {"data_source_id": TASKS, "member_data_source_id": MEMBERS, "channel_id": "C1"}
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "TEAM_ID=T1",
                "USER_IDS='[\"U1\"]'",
                "CHANNEL_IDS='[\"C1\"]'",
                f"NOTION_DATA_SOURCES='{json.dumps([TASKS, MEMBERS])}'",
                f"DAILY_TASK_REMINDER='{json.dumps(payload)}'",
            ]
        )
    )
    settings = Settings.load(path)
    assert settings.daily_task_reminder is not None
    assert settings.daily_task_reminder.hour == 9
    assert settings.daily_task_reminder.excluded_statuses == {"finish", "Done"}
    assert "daily_task_reminder" not in settings.agent_policy().model_dump()
    for override in ({"channel_id": "C2"}, {"hour": 24}, {"data_source_id": PERSON}):
        with pytest.raises(ValidationError):
            Settings.model_validate(
                {**settings.model_dump(), "daily_task_reminder": {**payload, **override}}
            )


def test_root_composition_wires_task_reminder_without_ai(
    board_runtime, monkeypatch, harness, tmp_path
):
    usecase, _, clock, slack, _, _, _, _, _, _ = board_runtime
    planning = Mock(skillbook=harness.skillbook)
    slack.auth_test.return_value = {"team_id": "T1", "user_id": "UBOT"}
    monkeypatch.setattr(composition, "build_clock", lambda: clock)
    monkeypatch.setattr(composition, "build_planning", lambda *a, **k: planning)
    monkeypatch.setattr(composition, "WebClient", lambda **k: slack)
    monkeypatch.setattr(composition, "App", Mock())
    monkeypatch.setattr(composition.Credentials, "from_authorized_user_file", Mock())
    build_board = Mock(return_value=usecase.board)
    monkeypatch.setattr(composition, "build_task_board", build_board)
    token_file = tmp_path / "google.json"
    token_file.write_text("{}")
    settings = Settings(
        team_id="T1",
        user_ids={"U1"},
        channel_ids={"C1"},
        notion_data_sources={TASKS, MEMBERS},
        database=tmp_path / "root.sqlite3",
        google_token_file=token_file,
        slack_bot_token="test",
        slack_app_token="test",
        notion_token="test",
        daily_task_reminder=usecase.config,
    )
    with ExitStack() as stack:
        _, agent = composition.build(settings, stack)
        agent.remind_tasks()
        agent.remind_schedule()
    build_board.assert_called_once()
    slack.chat_postMessage.assert_called_once()
    assert "担当者: <@U1>" in slack.chat_postMessage.call_args.kwargs["text"]
    planning.plan.assert_not_called()


@pytest.fixture
def overdue_runtime(board_runtime):
    usecase, restart, *rest = board_runtime
    config = usecase.config.model_copy(update={"overdue_alert_property": "期限アラート"})
    usecase.config = config
    usecase.board.repository.config = config

    def configured_restart():
        result = restart()
        result.config = config
        return result

    return (usecase, configured_restart, *rest)


def test_daily_summary_uses_alert_value_and_excludes_completed_tasks(overdue_runtime):
    usecase, restart, _, slack, rows, _, _, requests, _, _ = overdue_runtime
    rows[:] = [
        task(1),
        task(2, start="2000-01-01", alert="🚨 期限超過 🚨"),
        task(3, start="2000-01-01", alert=""),
        task(4, start="2000-01-01", alert="🚨 期限超過 🚨", status="finish"),
        task(5, start="2000-01-01", alert="🚨 期限超過 🚨", status="Done"),
        task(6, start="2000-01-01", alert="🚨 期限超過 🚨", archived=True),
        task(7, start="2099-01-01", alert="🚨 期限超過 🚨", people=[]),
        task(8, start="2000-01-01", alert="期限が近い"),
        task(9, status="Done"),
    ]
    usecase.execute()
    sent = slack.chat_postMessage.call_args.kwargs
    assert sent["channel"] == "C1"
    today, overdue = sent["text"].split("\n\n")
    assert today.startswith("今日のタスク（10/26）") and "タスク1" in today
    assert overdue.startswith("期限超過のタスク（10/26）")
    assert "タスク2" in overdue and "タスク7" in overdue
    assert "<@U1>" in overdue and "担当者: 未設定" in overdue
    assert all(f"タスク{i}" not in sent["text"] for i in (3, 4, 5, 6, 8, 9))
    queries = [
        json.loads(r.content) for r in requests if r.method == "POST" and TASKS in r.url.path
    ]
    overdue_filter = next(
        q["filter"]["and"]
        for q in queries
        if any(f["property"] == "期限アラート" for f in q["filter"]["and"])
    )
    assert {
        "property": "期限アラート",
        "formula": {"string": {"equals": "🚨 期限超過 🚨"}},
    } in overdue_filter
    assert all(f["property"] != "Dead" for f in overdue_filter)
    for status in ("finish", "Done"):
        assert {"property": "Status", "status": {"does_not_equal": status}} in overdue_filter
    restart().execute()
    slack.chat_postMessage.assert_called_once()


def test_overdue_without_today_repeats_daily_until_completed(overdue_runtime):
    usecase, restart, clock, slack, rows, _, _, _, _, _ = overdue_runtime
    rows[:] = [task(start="2000-01-01", alert="🚨 期限超過 🚨")]
    usecase.execute()
    assert slack.chat_postMessage.call_args.kwargs["text"].startswith("期限超過のタスク（10/26）")
    restart().execute()
    slack.chat_postMessage.assert_called_once()
    clock.value = "2026-10-27T09:00:00+09:00"
    restart().execute()
    assert slack.chat_postMessage.call_count == 2
    rows[0]["properties"]["Status"]["status"]["name"] = "Done"
    clock.value = "2026-10-28T09:00:00+09:00"
    restart().execute()
    assert slack.chat_postMessage.call_count == 2


def test_overdue_selection_does_not_depend_on_dead_property(overdue_runtime):
    usecase, _, _, _, rows, _, _, _, _, _ = overdue_runtime
    rows[:] = [task(alert="🚨 期限超過 🚨")]
    del rows[0]["properties"]["Dead"]
    tasks = usecase.board.overdue_tasks()
    assert len(tasks) == 1 and tasks[0].slack_user_ids == {"U1"}


@pytest.mark.parametrize(
    "failure", ["missing_alert", "wrong_type", "unmatched_person", "outside_channel"]
)
def test_overdue_read_or_identity_failure_does_not_send_partial_summary(overdue_runtime, failure):
    usecase, _, _, slack, rows, members, users, _, path, _ = overdue_runtime
    rows[:] = [task(1, people=[]), task(2, start="2000-01-01", alert="🚨 期限超過 🚨")]
    if failure == "missing_alert":
        del rows[1]["properties"]["期限アラート"]
    elif failure == "wrong_type":
        rows[1]["properties"]["期限アラート"]["formula"] = {"type": "boolean", "boolean": True}
    elif failure == "unmatched_person":
        users[PERSON]["name"] = "未知の担当者"
    else:
        members[0]["properties"]["Slack User ID"] = prop("rich_text", "U9")
    usecase.execute()
    slack.chat_postMessage.assert_not_called()
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT * FROM schedule_reminders").fetchall()


def test_empty_overdue_summary_does_not_post(overdue_runtime):
    usecase, restart, _, slack, rows, _, _, requests, _, _ = overdue_runtime
    rows[:] = [task(start="2000-01-01", alert="")]
    usecase.execute()
    count = len(requests)
    restart().execute()
    assert len(requests) == count
    slack.chat_postMessage.assert_not_called()
