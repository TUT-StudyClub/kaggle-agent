import json

import httpx

from modules.knowledge.infrastructure.external_services.notion import NotionRepository
from shared.infrastructure.http import JsonHttp

SOURCE = "00000000-0000-0000-0000-000000000001"
DATABASE = "00000000-0000-0000-0000-000000000002"
ROW = "00000000-0000-0000-0000-000000000003"


def test_search_allows_only_configured_data_sources_and_returns_database_link():
    def handler(request):
        if request.url.path.endswith("search"):
            assert "filter" not in json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "object": "data_source",
                            "id": SOURCE,
                            "title": [{"plain_text": "タスク"}],
                            "parent": {"type": "database_id", "database_id": DATABASE},
                        },
                        {"object": "data_source", "id": ROW, "title": [{"plain_text": "範囲外"}]},
                    ],
                    "has_more": False,
                },
            )
        assert request.url.path.endswith(DATABASE)
        return httpx.Response(200, json={"url": "https://www.notion.so/database"})

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        pages = NotionRepository(JsonHttp(c), set(), {}, {SOURCE}).search("タスク")
    assert len(pages) == 1 and pages[0].kind == "data_source"
    assert pages[0].title == "タスク" and pages[0].url == "https://www.notion.so/database"


def test_data_source_query_returns_schema_and_row_metadata_without_inventing_body():
    def handler(request):
        if request.url.path.endswith("query"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": ROW,
                            "url": "https://notion.so/row",
                            "properties": {"Status": {"type": "status"}},
                        },
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "object": "data_source",
                "id": SOURCE,
                "title": [],
                "properties": {},
                "url": "https://notion.so/database",
            },
        )

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        document = NotionRepository(JsonHttp(c), set(), {}, {SOURCE}).read(SOURCE)
    assert ROW in document.text and "本文は各ページを参照" in document.text
