import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from apps.slack.handlers.events import register_listeners
from modules.interaction.infrastructure.messaging.slack import SlackMessenger
from modules.knowledge.domain.models import Note
from modules.knowledge.infrastructure.external_services.notion import NotionRepository
from modules.planning.infrastructure.codex_cli import (
    CodexCLIPlanner,
    child_environment,
    planning_input,
    strict_schema,
)
from modules.planning.infrastructure.skills import load_skillbook
from modules.planning.public import Plan, PlanningRequest
from modules.scheduling.domain.models import TimeRange, free_slots
from modules.scheduling.infrastructure.external_services.google_calendar import (
    GoogleCalendarRepository,
)
from shared.errors import AgentError, UncertainWrite
from shared.infrastructure.http import JsonHttp
from tests.conftest import Harness

PAGE_ID = "00000000-0000-0000-0000-000000000001"
OTHER_ID = "00000000-0000-0000-0000-000000000002"


def period(start: str, end: str) -> TimeRange:
    return TimeRange.model_validate(
        {"start": f"2026-10-23T{start}:00+09:00", "end": f"2026-10-23T{end}:00+09:00"}
    )


def test_free_slots_merge_busy_clip_boundaries_and_keep_timezone():
    result = free_slots(
        period("09:00", "18:00"),
        [
            period("08:00", "10:00"),
            period("09:30", "11:00"),
            period("10:30", "12:00"),
            period("15:00", "20:00"),
        ],
        30,
    )
    assert result == [period("12:00", "15:00")]


@pytest.mark.parametrize(
    "payload",
    [
        {"start": "2026-10-23T10:00:00", "end": "2026-10-23T11:00:00"},
        {"start": "2026-10-23T11:00:00+09:00", "end": "2026-10-23T10:00:00+09:00"},
    ],
)
def test_dates_require_timezone_and_order(payload):
    with pytest.raises(ValidationError):
        TimeRange.model_validate(payload)


@pytest.mark.parametrize("kind", ["shell.exec", "notion.delete", "calendar.invite"])
def test_unknown_ai_actions_rejected(kind: str):
    with pytest.raises(ValidationError):
        Plan.model_validate({"action": {"kind": kind}})


def test_ai_cannot_supply_approval_or_identity():
    with pytest.raises(ValidationError):
        Plan.model_validate({"action": {"kind": "reply", "text": "ok", "approved": True}})


def test_secret_environment_not_passed_to_ai(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "NOTION_TOKEN",
        "SLACK_BOT_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CODEX_THREAD_ID",
    ):
        monkeypatch.setenv(name, "must-not-leak")
    assert "must-not-leak" not in child_environment().values()


def test_codex_tools_disabled_and_subscription_forced(tmp_path: Path):
    command = CodexCLIPlanner(load_skillbook())._command(tmp_path / "schema", tmp_path / "out")
    assert 'forced_login_method="chatgpt"' in command
    assert "read-only" in command
    for feature in ("shell_tool", "apps", "plugins", "multi_agent", "computer_use"):
        index = command.index(feature)
        assert command[index - 1] == "--disable"
    assert "--ignore-user-config" in command


def test_codex_model_and_reasoning_are_fixed(tmp_path: Path):
    command = CodexCLIPlanner(load_skillbook())._command(tmp_path / "schema", tmp_path / "out")
    assert command[command.index("--model") + 1] == "gpt-6-luna"
    assert 'model_reasoning_effort="medium"' in command
    assert "--ignore-user-config" in command


@pytest.mark.parametrize("mode", ["live", "disabled"])
@pytest.mark.parametrize(
    "event_type", ["web_search", "command_execution", "mcp_tool_call", "file_change"]
)
def test_codex_only_accepts_configured_public_web_tool_events(monkeypatch, mode, event_type):
    book = load_skillbook()
    planner = CodexCLIPlanner(
        book.model_copy(update={"policy": book.policy.model_copy(update={"web_search": mode})})
    )
    monkeypatch.setattr(planner, "check_auth", lambda: None)
    process = MagicMock()
    process.__enter__.return_value = process
    process.returncode = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr("modules.planning.infrastructure.codex_cli.subprocess.Popen", popen)

    def respond(input_text, timeout):
        command = popen.call_args.args[0]
        assert f'web_search="{mode}"' in command
        host = command.index("code_mode_host")
        assert command[host - 1] == ("--enable" if mode == "live" else "--disable")
        for feature in ("shell_tool", "apps", "plugins", "multi_agent"):
            assert command[command.index(feature) - 1] == "--disable"
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"action":{"kind":"reply","text":"根拠: https://example.com/rules"}}')
        return json.dumps({"type": "item.completed", "item": {"type": event_type}}), ""

    process.communicate.side_effect = respond
    request = PlanningRequest(
        instruction="提出ルールを調べて",
        now="2026-10-22T19:00:00+09:00",
        context="{}",
        resources={},
        thread="[]",
        observations=[],
    )
    if mode == "live" and event_type == "web_search":
        assert planner.plan(request).action.kind == "reply"
    else:
        with pytest.raises(AgentError) as caught:
            planner.plan(request)
        assert caught.value.code == "ai_invalid_output"


def test_codex_passes_rules_as_developer_instructions_and_input_separately(monkeypatch):
    planner = CodexCLIPlanner(load_skillbook())
    monkeypatch.setattr(planner, "check_auth", lambda: None)
    process = MagicMock()
    process.__enter__.return_value = process
    process.returncode = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr("modules.planning.infrastructure.codex_cli.subprocess.Popen", popen)

    def respond(input_text: str, timeout: int):
        command = popen.call_args.args[0]
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"action": {"kind": "reply", "text": "確認済み"}}')
        return "", ""

    process.communicate.side_effect = respond
    request = PlanningRequest(
        instruction="動作確認",
        now="2026-10-20T23:00:00+09:00",
        context="{}",
        resources={},
        thread="[]",
        observations=[],
    )
    assert planner.plan(request).action.kind == "reply"
    command = popen.call_args.args[0]
    override = next(value for value in command if value.startswith("developer_instructions="))
    assert json.loads(override.split("=", 1)[1]) == planner.instructions
    for skill in load_skillbook().skills:
        assert skill.rules in planner.instructions and skill.instructions in planner.instructions
    process.communicate.assert_called_once_with(
        planning_input(request), timeout=planner.timeout_seconds
    )


def test_planning_input_exposes_pending_request_and_thread_as_structured_data():
    previous = {
        "pending_clarification": {
            "requests": ["Pythonの仮想環境の作り方を調べて"],
            "question": "どの情報源で確認する？",
        }
    }
    thread = [{"user": "UEVAL", "text": "Pythonの仮想環境の作り方を調べて"}]
    request = PlanningRequest(
        instruction="公式ドキュメントで",
        now="2026-10-22T20:00:00+09:00",
        context=json.dumps(previous),
        thread=json.dumps(thread),
        resources={},
        observations=[],
    )
    payload = json.loads(planning_input(request).split("\n", 1)[1])
    assert payload["context"] == previous
    assert payload["thread"] == thread
    assert payload["instruction"] == "公式ドキュメントで"
    plain = request.model_copy(update={"thread": "許可された利用者の会話"})
    assert json.loads(planning_input(plain).split("\n", 1)[1])["thread"] == plain.thread


def test_schema_contains_no_optional_untyped_action_bag():
    schema = strict_schema(Plan.model_json_schema())
    assert "oneOf" not in json.dumps(schema)
    assert "discriminator" not in json.dumps(schema)
    for value in schema["$defs"].values():
        if value.get("type") == "object":
            assert value["additionalProperties"] is False
            assert set(value["required"]) == set(value["properties"])


def test_http_read_retries_rate_limit():
    calls, delays = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        assert JsonHttp(c, sleep=delays.append).request("GET", "read") == {"ok": True}
    assert len(calls) == 2 and delays == [2]


def test_uncertain_write_is_never_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("response lost")

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(UncertainWrite):
            JsonHttp(c).request("POST", "create", json={})
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,code", [(401, "permission"), (403, "permission"), (412, "conflict"), (404, "not_found")]
)
def test_provider_errors_do_not_expose_response_body(status: int, code: str):
    with httpx.Client(
        base_url="https://test.invalid/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="secret-token"),
        ),
    ) as c:
        with pytest.raises(AgentError) as caught:
            JsonHttp(c).request("GET", "resource")
    assert caught.value.code == code
    assert "secret-token" not in str(caught.value)


def test_freebusy_partial_error_is_not_reported_as_free():
    with httpx.Client(
        base_url="https://test.invalid/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "calendars": {
                        "id": {"errors": [{"reason": "forbidden"}]},
                    }
                },
            ),
        ),
    ) as c:
        repo = GoogleCalendarRepository(JsonHttp(c), {"personal": "id"})
        with pytest.raises(AgentError, match="Busy"):
            repo.busy(["personal"], period("09:00", "18:00"))


def test_calendar_unconfigured_alias_does_not_make_http_request():
    with httpx.Client(
        base_url="https://test.invalid/",
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Unauthorized HTTP request"),
        ),
    ) as c:
        with pytest.raises(AgentError, match="別名"):
            GoogleCalendarRepository(JsonHttp(c), {}).events("arbitrary", period("09:00", "18:00"))


def test_notion_append_preserves_body_and_uses_marker_for_replay():
    blocks = [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "既存本文"}]}}]
    writes = []

    def handler(request):
        if request.url.path.endswith("/children"):
            if request.method == "GET":
                return httpx.Response(200, json={"results": blocks, "has_more": False})
            writes.append(request)
            blocks.extend(json.loads(request.content)["children"])
            return httpx.Response(200, json={"results": blocks})
        return httpx.Response(200, json={"id": PAGE_ID, "url": "https://notion.so/test"})

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        repo = NotionRepository(JsonHttp(c), {PAGE_ID}, {})
        note = Note(title="追記", body="決定事項", source_url="https://slack.com/source")
        repo.append(PAGE_ID, note, "job1")
        repo.append(PAGE_ID, note, "job1")
    assert len(writes) == 1
    assert blocks[0]["paragraph"]["rich_text"][0]["plain_text"] == "既存本文"
    assert "https://slack.com/source" in json.dumps(blocks)


def test_notion_unauthorized_page_not_read():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"parent": {"type": "workspace", "workspace": True}})

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(AgentError, match="範囲外"):
            NotionRepository(JsonHttp(c), {PAGE_ID}, {}).read(OTHER_ID)
    assert not any("children" in path for path in paths)


def test_notion_update_rejects_block_in_other_page():
    writes = []

    def handler(request):
        if request.method != "GET":
            writes.append(request)
        return httpx.Response(
            200,
            json={
                "type": "paragraph",
                "parent": {
                    "type": "page_id",
                    "page_id": OTHER_ID,
                },
            },
        )

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        with pytest.raises(AgentError, match="属して"):
            NotionRepository(JsonHttp(c), {PAGE_ID}, {}).update_block(
                PAGE_ID,
                OTHER_ID,
                "before",
                "after",
                "job",
            )
    assert not writes


@pytest.mark.parametrize(
    "event",
    [
        {"bot_id": "B1", "text": "bot"},
        {"subtype": "message_changed", "text": "edit"},
        {"user": "BOT", "text": "own"},
    ],
)
def test_bot_and_changed_events_ignored(harness: Harness, event):
    listeners = {}
    app = Mock()
    app.event.side_effect = lambda kind: lambda callback: listeners.update({kind: callback})
    register_listeners(app, harness.agent, "BOT")
    listeners["message"]({"team_id": "T1"}, event)
    assert not harness.agent.run_once()


def test_mention_and_message_delivery_are_deduplicated(harness: Harness):
    listeners = {}
    app = Mock()
    app.event.side_effect = lambda kind: lambda callback: listeners.update({kind: callback})
    register_listeners(app, harness.agent, "BOT")
    body = {"team_id": "T1"}
    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "ts": "1000.000001",
        "text": "<@BOT> こんにちは",
    }
    listeners["app_mention"](body, event)
    listeners["message"](body, event | {"type": "message"})
    assert harness.jobs.claim() is not None
    assert harness.jobs.claim() is None


def test_slack_reply_uses_standard_markdown(harness: Harness):
    client = Mock()
    text = (
        "## 次の予定\n\n**キックオフ**\n\n- 18:00開始\n\n"
        "[予定を開く](https://calendar.google.com/event/test)\n\n"
        "```python\nprint('hello')\n```"
    )
    SlackMessenger(client, harness.policy).send(harness.source, text)
    client.chat_postMessage.assert_called_once_with(
        channel=harness.source.channel_id,
        thread_ts=harness.source.thread_ts,
        markdown_text=text,
        unfurl_links=False,
        unfurl_media=False,
    )


def test_slack_long_preview_keeps_full_body_and_approval_command(harness: Harness):
    client = Mock()
    text = "```json\n" + "あ" * 12000 + "\n```\n同じスレッドで `承認 job-id` と返信"
    SlackMessenger(client, harness.policy).send(harness.source, text)
    client.chat_postMessage.assert_called_once_with(
        channel=harness.source.channel_id,
        thread_ts=harness.source.thread_ts,
        text=text,
        mrkdwn=True,
        unfurl_links=False,
        unfurl_media=False,
    )


def test_slack_thread_excludes_unapproved_actors(harness: Harness):
    client = Mock()
    client.conversations_replies.return_value = {
        "messages": [
            {"user": "U1", "ts": "1", "text": "許可本文"},
            {"user": "U2", "ts": "2", "text": "非許可本文"},
        ]
    }
    text = SlackMessenger(client, harness.policy).thread(harness.source)
    assert "許可本文" in text and "非許可本文" not in text
