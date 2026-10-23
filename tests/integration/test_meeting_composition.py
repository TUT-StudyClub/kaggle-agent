import json
import sqlite3
from contextlib import ExitStack

import httpx
import pytest
from google.oauth2.credentials import Credentials
from slack_sdk import WebClient

import composition
from config import Settings
from modules.interaction.domain.models import IncomingMessage, Source
from modules.planning.infrastructure.codex_cli import CodexCLIPlanner
from modules.planning.public import (
    CreateMeeting,
    ListAnnouncementChannels,
    MeetingAnnouncement,
    Plan,
)
from modules.scheduling.public import CreateEvent, ListCalendars
from tests.contract.test_member_directory import SOURCE, row, schema
from tests.support import draft


@pytest.mark.parametrize("reaction_scope", [False, True])
def test_real_composition_invites_and_announces_only_after_one_approval(
    tmp_path, monkeypatch, reaction_scope
):
    event = {}
    writes = []

    class Auth(dict):
        headers = {
            "x-oauth-scopes": "chat:write,reactions:read" if reaction_scope else "chat:write"
        }

    class SlackApp:
        def __init__(self, **kwargs):
            pass

        def event(self, name):
            return lambda callback: callback

    def slack_api(self, method, **kwargs):
        if method == "auth.test":
            return Auth(ok=True, team_id="T1", user_id="UBOT")
        args = kwargs.get("json") or kwargs.get("params") or {}
        if method == "assistant.threads.setStatus":
            return {"ok": True}
        if method == "conversations.replies":
            return {"ok": True, "messages": []}
        if method == "conversations.info":
            return {"ok": True, "channel": {"id": "C1", "name": "general", "is_member": True}}
        if method == "chat.postMessage":
            assert event and args["channel"] == "C1"
            assert args["text"].startswith("<!channel>\n")
            assert "thread_ts" not in args
            writes.append("slack")
            return {"ok": True, "ts": "2000.000001"}
        raise AssertionError(method)

    def respond(request):
        if request.url.path == f"/v1/data_sources/{SOURCE}":
            result = schema()
            result["properties"]["Calendar Emails"] = {"type": "rich_text"}
        elif request.url.path == f"/v1/data_sources/{SOURCE}/query":
            member = row()
            member["properties"]["Calendar Emails"] = {
                "type": "rich_text",
                "rich_text": [{"plain_text": "guest@example.com"}],
            }
            result = {"results": [member]}
        elif "/calendarList/" in request.url.path:
            result = {"accessRole": "owner", "summary": "個人"}
        elif request.url.path == "/calendar/v3/calendars/test-calendar":
            result = {
                "labelProperties": {
                    "eventLabels": [
                        {"id": "label-a", "name": "田中"},
                        {"id": "unnamed", "backgroundColor": "#7cb342"},
                        {"id": "kaggle", "name": "Kaggle", "backgroundColor": "#7cb342"},
                    ]
                }
            }
        elif (
            request.url.path == "/calendar/v3/calendars/test-calendar/events"
            and request.method == "POST"
        ):
            assert request.url.params["sendUpdates"] == "all"
            assert request.url.params["eventLabelVersion"] == "1"
            event.update(json.loads(request.content) | {"etag": "v1"})
            assert event["attendees"] == [{"email": "guest@example.com"}]
            assert event["eventLabelId"] == "kaggle"
            writes.append("calendar")
            result = event
        elif "/events/" in request.url.path and request.method == "GET":
            assert request.url.params["eventLabelVersion"] == "1"
            return httpx.Response(200, json=event) if event else httpx.Response(404)
        else:
            raise AssertionError(str(request.url))
        return httpx.Response(200, json=result)

    client_type = httpx.Client
    monkeypatch.setattr(
        composition.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(composition, "App", SlackApp)
    monkeypatch.setattr(WebClient, "api_call", slack_api)
    monkeypatch.setattr(CodexCLIPlanner, "check_auth", lambda self: None)

    def plan(self, request):
        assert request.resources["calendars"]["personal"]["creation_label"]["id"] == "kaggle"
        if not any("calendars" in observation for observation in request.observations):
            return Plan(action=ListCalendars())
        if not any("announcement_channels" in observation for observation in request.observations):
            return Plan(action=ListAnnouncementChannels())
        return Plan(
            action=CreateMeeting(
                event=CreateEvent(calendar_alias="personal", draft=draft(), guest_user_ids=["U2"]),
                announcement=MeetingAnnouncement(
                    channel_id="C1", text="面談を開催します。", mention_channel=True
                ),
            )
        )

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
            "calendars": {
                "personal": {
                    "id": "test-calendar",
                    "writable": True,
                    "member_data_source_id": SOURCE,
                    "creation_label": {
                        "id": "kaggle",
                        "name": "Kaggle",
                        "background_color": "#7cb342",
                    },
                }
            },
            "database": tmp_path / "agent.sqlite3",
            "google_token_file": token_file,
            "slack_bot_token": "xoxb-test",
            "slack_app_token": "xapp-test",
            "notion_token": "notion-test",
        }
    )
    source = Source(team_id="T1", channel_id="C1", thread_ts="1000.000001", user_id="U1")
    with ExitStack() as stack:
        _, agent = composition.build(settings, stack)
        job_id = agent.receive(
            IncomingMessage(
                event_id="request",
                source=source,
                text="田中を招待して予定作成、generalに@channelで周知",
            )
        )
        assert job_id is not None
        agent.run_once()
        status = agent.status(job_id)
        assert status is not None and status.status == "awaiting_approval"
        with sqlite3.connect(settings.database) as db:
            preview = db.execute("SELECT text FROM notifications").fetchone()[0]
        assert "同じスレッドで" not in preview
        assert "リアクションでも承認" not in preview
        assert "分以内" not in preview
        assert "イベントラベル: Kaggle (#7cb342)" in preview
        assert not writes
        agent.receive(IncomingMessage(event_id="approval", source=source, text=f"承認 {job_id}"))
        agent.run_once()
        status = agent.status(job_id)
        assert status is not None and status.status == "succeeded"
        assert writes == ["calendar", "slack"]
