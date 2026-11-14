import json
from datetime import date

import httpx
import pytest

from modules.interaction.infrastructure.notion_retro import NotionRetroDocuments
from shared.errors import AgentError
from shared.infrastructure.http import JsonHttp

SOURCE = "00000000-0000-4000-8000-000000000001"
TEMPLATE = "00000000-0000-4000-8000-000000000002"
PAGE = "00000000-0000-4000-8000-000000000003"
USER = "00000000-0000-4000-8000-000000000004"


def test_template_creation_uses_event_day_and_expected_properties():
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": PAGE, "url": "https://notion.test/page"})

    with httpx.Client(base_url="https://notion.test/", transport=httpx.MockTransport(respond)) as c:
        docs = NotionRetroDocuments(JsonHttp(c), SOURCE, TEMPLATE)
        assert docs.create(date(2026, 11, 21), (USER,)) == (PAGE, "https://notion.test/page")
    body = sent[0]
    assert body["parent"]["data_source_id"] == SOURCE
    assert body["template"]["template_id"] == TEMPLATE
    assert body["template"]["timezone"] == "Asia/Tokyo"
    props = body["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "[Retro & Planning] 2026/11/21"
    assert props["Date"]["date"]["start"] == "2026-11-21"
    assert props["Type"]["select"]["name"] == "Retro & Planning"
    assert props["User"]["people"] == [{"id": USER}]


def test_member_db_mapping_must_cover_all_members():
    def respond(request):
        if "/data_sources/" in request.url.path:
            schema = {
                name: {"type": kind}
                for name, kind in {
                    "Name": "title",
                    "Date": "date",
                    "User": "people",
                    "Type": "select",
                }.items()
            }
            return httpx.Response(200, json={"properties": schema})
        return httpx.Response(200, json={})

    with httpx.Client(base_url="https://notion.test/", transport=httpx.MockTransport(respond)) as c:
        docs = NotionRetroDocuments(JsonHttp(c), SOURCE, TEMPLATE)
        assert docs.members([{"name": "A", "notion_user_ids": [USER]}]) == (USER,)
        with pytest.raises(AgentError, match="Member DB"):
            docs.members([{"name": "B", "notion_user_ids": []}])


def test_waits_for_template_blocks_before_reporting_document_ready(monkeypatch):
    polls = 0
    monkeypatch.setattr(
        "modules.interaction.infrastructure.notion_retro.time.sleep", lambda _: None
    )

    def respond(request):
        nonlocal polls
        if request.url.path == f"/pages/{PAGE}":
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "Name": {"title": [{"plain_text": "[Retro & Planning] 2026/11/21"}]},
                        "Date": {"date": {"start": "2026-11-21"}},
                        "Type": {"select": {"name": "Retro & Planning"}},
                        "User": {"people": [{"id": USER}]},
                    }
                },
            )
        if request.url.path == f"/blocks/{TEMPLATE}/children":
            return httpx.Response(
                200, json={"results": [{"type": "heading_2"}, {"type": "child_database"}]}
            )
        polls += 1
        result = [{"type": "heading_2"}]
        if polls > 1:
            result.append({"type": "child_database"})
        return httpx.Response(200, json={"results": result})

    with httpx.Client(base_url="https://notion.test/", transport=httpx.MockTransport(respond)) as c:
        docs = NotionRetroDocuments(JsonHttp(c), SOURCE, TEMPLATE)
        assert docs.ready(PAGE, date(2026, 11, 21), (USER,))
    assert polls == 2
