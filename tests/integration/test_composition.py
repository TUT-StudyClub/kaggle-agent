import sqlite3
from contextlib import ExitStack

import httpx
import pytest
from google.oauth2.credentials import Credentials
from slack_sdk import WebClient

import composition
from config import Settings
from modules.interaction.domain.models import JobStatus
from modules.planning.infrastructure.codex_cli import CodexCLIPlanner
from modules.planning.public import Plan, Reply


@pytest.mark.parametrize("kind", ["notion.search", "notion.read", "calendar.list"])
def test_slack_input_reaches_composed_modules_and_persists_reply(tmp_path, monkeypatch, kind):
    """実際の組み立てを通し、外部I/Oだけを模擬する。"""
    callbacks = {}
    requests = []
    competition = "3fe03e58-e494-8077-a2d4-000b2dc515fd"

    class SlackApp:
        def __init__(self, **kwargs):
            pass

        def event(self, name):
            def register(callback):
                callbacks[name] = callback

            return register

    def slack_api(self, method, **kwargs):
        if method == "auth.test":
            return {"ok": True, "team_id": "T1", "user_id": "UBOT"}
        if method == "assistant.threads.setStatus":
            return {"ok": True}
        if method == "conversations.replies":
            return {"ok": True, "messages": []}
        if method == "chat.getPermalink":
            return {"ok": True, "permalink": "https://example.slack.com/thread"}
        raise AssertionError(f"Unexpected Slack API: {method}")

    def http_response(request):
        requests.append((request.method, request.url.host, request.url.path))
        if request.url.host == "api.notion.com" and request.url.path == "/v1/search":
            assert request.method == "POST"
            return httpx.Response(200, json={"results": [], "has_more": False})
        if request.url.path == f"/v1/data_sources/{competition}/query":
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "row", "url": "https://notion.so/row", "properties": {}}],
                    "has_more": False,
                },
            )
        assert request.method == "GET"
        if request.url.path == f"/v1/data_sources/{competition}":
            return httpx.Response(
                200,
                json={
                    "object": "data_source",
                    "id": competition,
                    "title": [{"plain_text": "Competition DB"}],
                    "properties": {},
                    "url": "https://notion.so/competition",
                },
            )
        if "/calendarList/" in request.url.path:
            return httpx.Response(200, json={"accessRole": "owner", "summary": "個人"})
        if request.url.path == "/calendar/v3/calendars/test-calendar":
            return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected HTTP API: {request.url}")

    client_type = httpx.Client
    monkeypatch.setattr(
        composition.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(http_response), **kwargs),
    )
    monkeypatch.setattr(composition, "App", SlackApp)
    monkeypatch.setattr(WebClient, "api_call", slack_api)
    monkeypatch.setattr(CodexCLIPlanner, "check_auth", lambda self: None)

    def plan(self, request):
        if kind == "notion.read":
            assert request.resources["notion_competition_data_source_id"] == competition
            assert competition in request.resources["notion_data_sources"]
        if request.observations:
            if kind == "notion.read":
                assert "Competition DB" == request.observations[0]["page"]["title"]
                assert '"id": "row"' in request.observations[0]["page"]["text"]
                return Plan(action=Reply(text="https://notion.so/competition"))
            return Plan(action=Reply(text="確認しました。"))
        arguments = {"query": "確認"} if kind == "notion.search" else {}
        if kind == "notion.read":
            arguments = {"page_id": competition}
        return Plan.model_validate({"action": {"kind": kind, **arguments}})

    monkeypatch.setattr(CodexCLIPlanner, "plan", plan)
    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *_: Credentials(token="test")
    )
    token_file = tmp_path / "google.json"
    token_file.write_text("{}")
    settings = Settings.model_validate(
        {
            "team_id": "T1",
            "user_ids": ["U1"],
            "channel_ids": ["C1"],
            "notion_parents": {"notes": "00000000-0000-0000-0000-000000000001"},
            "calendars": {"personal": {"id": "test-calendar"}},
            "database": tmp_path / "agent.sqlite3",
            "google_token_file": token_file,
            "slack_bot_token": "xoxb-test",
            "slack_app_token": "xapp-test",
            "notion_token": "notion-test",
            "notion_competition_data_source_id": competition if kind == "notion.read" else None,
        }
    )
    with ExitStack() as stack:
        _, agent = composition.build(settings, stack)
        callbacks["message"](
            body={"team_id": "T1"},
            event={
                "user": "U1",
                "channel": "C1",
                "channel_type": "im",
                "ts": "1.0",
                "text": "確認して",
            },
        )
        assert agent.run_once()
        assert not agent.run_once()

    with sqlite3.connect(settings.database) as db:
        job_id, status = db.execute("SELECT id,status FROM jobs").fetchone()
        assert status == JobStatus.SUCCEEDED
        expected = "https://notion.so/competition" if kind == "notion.read" else "確認しました。"
        assert db.execute("SELECT text FROM notifications").fetchall() == [(expected,)]
    status = agent.status(job_id)
    assert status is not None and status.status == JobStatus.SUCCEEDED
    assert requests
