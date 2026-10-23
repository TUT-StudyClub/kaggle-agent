import json
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from modules.knowledge.public import SearchPages
from modules.planning.public import Plan
from modules.scheduling.application.services.member_mapping import MemberMappingService
from modules.scheduling.composition import compose_scheduling
from modules.scheduling.domain.models import CalendarMember, EventLabel
from modules.scheduling.infrastructure.external_services.notion_members import NotionMemberDirectory
from modules.scheduling.public import CalendarConfig, ListCalendars, ListEvents
from shared.errors import AgentError
from shared.infrastructure.http import JsonHttp
from tests.conftest import Harness
from tests.support import period

SOURCE = "00000000-0000-0000-0000-000000000001"


def schema() -> dict:
    return {
        "properties": {
            "Name": {"type": "title"},
            "Slack User ID": {"type": "rich_text"},
            "Calendar Label ID": {"type": "rich_text"},
        }
    }


def row(label_id="label-a", user_id="U2", **extra) -> dict:
    props = {}
    for name, kind, value in (
        ("Name", "title", "田中"),
        ("Slack's name", "rich_text", "Example Tanaka"),
        ("Slack User ID", "rich_text", user_id),
        ("Calendar Label ID", "rich_text", label_id),
    ):
        props[name] = {"type": kind, kind: [{"plain_text": value}]}
    return {"properties": props, "url": "https://notion.so/member", **extra}


def test_directory_reads_only_configured_source_and_paginates_active_rows():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.startswith(f"/data_sources/{SOURCE}")
        if request.method == "GET":
            return httpx.Response(200, json=schema())
        assert request.method == "POST"
        payload = json.loads(request.content)
        if "start_cursor" not in payload:
            return httpx.Response(
                200,
                json={
                    "results": [row(), row(archived=True)],
                    "has_more": True,
                    "next_cursor": "page2",
                },
            )
        assert payload["start_cursor"] == "page2"
        return httpx.Response(
            200,
            json={"results": [row("label-b", "U3"), row(in_trash=True)], "has_more": False},
        )

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        directory = NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE})
        members = directory.members("personal")
        assert [(m.label_id, m.slack_user_id) for m in members] == [
            ("label-a", "U2"),
            ("label-b", "U3"),
        ]
        assert members[0].name == "田中" and members[0].slack_name == "Example Tanaka"
        with pytest.raises(AgentError, match="設定"):
            directory.members("unconfigured")
    assert len(calls) == 3


@pytest.mark.parametrize("label,user", [("", "U2"), ("label-a", ""), ("label-a", "<@U2>")])
def test_incomplete_row_is_rejected_without_inference_from_name_or_color(label, user):
    def handler(request):
        payload = schema() if request.method == "GET" else {"results": [row(label, user)]}
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AgentError, match="不備"):
            NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}).members("personal")


def test_schema_error_stops_before_query():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"properties": {}})

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AgentError, match="列と種類"):
            NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}).members("personal")


def test_incomplete_pagination_is_never_used_as_a_complete_mapping():
    def handler(request):
        payload = schema() if request.method == "GET" else {"results": [row()], "has_more": True}
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AgentError, match="最後まで"):
            NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}).members("personal")


def test_same_service_reloads_db_and_follows_changed_label_for_same_slack_user(harness: Harness):
    current = [row()]
    queries = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=schema())
        queries.append(request)
        return httpx.Response(200, json={"results": current, "has_more": False})

    base = harness.calendar.event("personal", "event-a")
    for label in ("label-a", "label-b"):
        harness.calendar.stored[label] = base.model_copy(update={"id": label, "label_id": label})
    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        service = compose_scheduling(
            harness.calendar,
            set(),
            member_directory=NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}),
        )
        action = ListEvents(calendar_alias="personal", period=period(), slack_user_id="U2")
        first = service.execute(action, "first")["events"]
        current[:] = [row("label-b")]
        second = service.execute(action, "second")["events"]
    assert isinstance(first, list) and isinstance(second, list)
    assert first[0]["id"] == "label-a" and second[0]["id"] == "label-b"
    assert second[0]["member"]["name"] == "田中"
    assert len(queries) == 2 and harness.calendar.writes == 0


@pytest.mark.parametrize("duplicate", [row("label-b", "U2"), row("label-a", "U3")])
def test_duplicate_slack_or_label_ids_stop_instead_of_picking_first(harness: Harness, duplicate):
    def handler(request):
        payload = (
            schema()
            if request.method == "GET"
            else {"results": [row(), duplicate], "has_more": False}
        )
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        service = compose_scheduling(
            harness.calendar,
            set(),
            member_directory=NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}),
        )
        with pytest.raises(AgentError, match="重複"):
            service.execute(ListEvents(calendar_alias="personal", period=period()), "read")


def test_outage_does_not_fall_back_to_static_mapping_or_fetch_events(harness: Harness, monkeypatch):
    def handler(request):
        return httpx.Response(403, text="private-credential-error")

    event_reads = Mock(side_effect=AssertionError("Must not query with stale mapping"))
    monkeypatch.setattr(harness.calendar, "events", event_reads)
    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        service = compose_scheduling(
            harness.calendar,
            set(),
            {"personal": {"old-label": "U2"}},
            NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}),
        )
        with pytest.raises(AgentError) as caught:
            service.execute(
                ListEvents(calendar_alias="personal", period=period(), slack_user_id="U2"), "read"
            )
    assert caught.value.code == "permission" and "private-credential" not in str(caught.value)
    event_reads.assert_not_called()


@pytest.mark.parametrize(
    "label,user,code",
    [("label-old", "U2", "member_mapping_changed"), (None, "U3", "member_not_found")],
)
def test_stale_or_missing_identity_does_not_return_someone_elses_events(harness, label, user, code):
    def handler(request):
        payload = schema() if request.method == "GET" else {"results": [row()]}
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        service = compose_scheduling(
            harness.calendar,
            set(),
            member_directory=NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}),
        )
        with pytest.raises(AgentError) as caught:
            service.execute(
                ListEvents(
                    calendar_alias="personal", period=period(), label_id=label, slack_user_id=user
                ),
                "read",
            )
    assert caught.value.code == code


@pytest.mark.parametrize("prior_reads", [0, 8])
def test_agent_refreshes_directory_before_using_a_stale_calendar_action(
    harness, monkeypatch, prior_reads
):
    member = CalendarMember(name="田中", slack_user_id="U2", label_id="label-a")
    directory = Mock(spec=NotionMemberDirectory)
    directory.calendar_aliases = frozenset({"personal"})
    directory.members.return_value = [member]
    harness.configure(member_directory=directory)
    harness.configure(
        policy=harness.policy.model_copy(
            update={
                "calendars": {"personal": CalendarConfig(id="id", member_data_source_id=SOURCE)}
            }
        )
    )
    calendar = harness.calendar.calendars()[0].model_copy(
        update={"labels": [EventLabel(id="label-a", name="田中")]}
    )
    monkeypatch.setattr(harness.calendar, "calendars", lambda: [calendar])
    harness.planner.steps = [
        *(Plan(action=SearchPages(query=f"対象{index}")) for index in range(prior_reads)),
        Plan(action=ListEvents(calendar_alias="personal", period=period(), label_id="stale")),
        Plan(action=ListEvents(calendar_alias="personal", period=period(), slack_user_id="U2")),
        Plan.model_validate({"action": {"kind": "reply", "text": "該当する予定はありません。"}}),
    ]
    harness.submit("<@U2>の予定")
    harness.agent.run_once()
    latest = harness.planner.requests[prior_reads + 1]
    calendars = latest.observations[prior_reads]["calendars"]
    assert isinstance(calendars, list)
    assert calendars[0]["members"][0]["slack_user_id"] == "U2"
    assert directory.members.call_count == 2  # 対応一覧と、照会時点の再読。
    assert harness.calendar.writes == 0


def test_non_calendar_reply_does_not_read_member_db(harness):
    directory = Mock(spec=NotionMemberDirectory)
    harness.configure(member_directory=directory)
    harness.planner.steps = [
        Plan.model_validate({"action": {"kind": "reply", "text": "こんにちは"}})
    ]
    harness.submit("こんにちは")
    harness.agent.run_once()
    directory.members.assert_not_called()


def test_fixed_mapping_and_db_cannot_be_configured_together():
    with pytest.raises(ValidationError, match="同時"):
        CalendarConfig(id="id", member_data_source_id=SOURCE, label_users={"label-a": "U2"})


def test_unknown_label_in_db_is_reported(harness):
    directory = Mock(spec=NotionMemberDirectory)
    directory.calendar_aliases = frozenset({"personal"})
    directory.members.return_value = [
        CalendarMember(name="田中", slack_user_id="U2", label_id="missing")
    ]
    service = compose_scheduling(harness.calendar, set(), member_directory=directory)
    with pytest.raises(AgentError, match="対象カレンダーにありません"):
        service.execute(ListCalendars(), "read")


@pytest.mark.parametrize(
    "emails",
    [
        "First@EXAMPLE.COM, second@example.com\nthird@example.com",
        "  • First@EXAMPLE.COM\n  • second@example.com\n  • third@example.com",
        "- First@EXAMPLE.COM\n* second@example.com\n・third@example.com",
    ],
)
def test_calendar_email_aliases_are_parsed_and_remain_json_serializable(emails):
    member_row = row()
    member_row["properties"]["Calendar Emails"] = {
        "type": "rich_text",
        "rich_text": [{"plain_text": emails}],
    }

    def handler(request):
        value = schema() if request.method == "GET" else {"results": [member_row]}
        if request.method == "GET":
            value["properties"]["Calendar Emails"] = {"type": "rich_text"}
        return httpx.Response(200, json=value)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        members = NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}).members("personal")
    assert members[0].calendar_emails == {
        "first@example.com",
        "second@example.com",
        "third@example.com",
    }
    json.dumps(members[0].model_dump(mode="json"))


def test_bulleted_mailto_spans_do_not_block_label_based_schedule_reads(harness):
    member_row = row()
    # 実際のNotionと同じく、改行・次の箇条書き記号がmailtoのspan内に入る形を再現する。
    member_row["properties"]["Calendar Emails"] = {
        "type": "rich_text",
        "rich_text": [
            {"plain_text": "  • "},
            {"plain_text": "first@example.com\n  • ", "href": "mailto:first@example.com"},
            {"plain_text": "second@example.com", "href": "mailto:second@example.com"},
        ],
    }

    def handler(request):
        value = schema() if request.method == "GET" else {"results": [member_row]}
        if request.method == "GET":
            value["properties"]["Calendar Emails"] = {"type": "rich_text"}
        return httpx.Response(200, json=value)

    base = harness.calendar.event("personal", "event-a")
    harness.calendar.stored["event-a"] = base.model_copy(update={"label_id": "label-a"})
    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        service = compose_scheduling(
            harness.calendar,
            set(),
            member_directory=NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}),
        )
        result = service.execute(ListEvents(calendar_alias="personal", period=period()), "read")
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["slack_user_id"] == "U2"
    assert set(events[0]["member"]["calendar_emails"]) == {
        "first@example.com",
        "second@example.com",
    }


@pytest.mark.parametrize("value", ["not-an-email", "first@example.com extra-text"])
def test_invalid_calendar_email_reports_the_email_column_without_including_values(value):
    member_row = row()
    member_row["properties"]["Calendar Emails"] = {
        "type": "rich_text",
        "rich_text": [{"plain_text": value}],
    }

    def handler(request):
        payload = schema() if request.method == "GET" else {"results": [member_row]}
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AgentError) as caught:
            NotionMemberDirectory(JsonHttp(client), {"personal": SOURCE}).members("personal")
    assert caught.value.code == "member_email_invalid"
    assert "Calendar Emails" in caught.value.message
    assert value not in caught.value.message


def test_duplicate_calendar_email_is_not_silently_assigned_to_first_person(harness):
    directory = Mock(spec=NotionMemberDirectory)
    directory.calendar_aliases = frozenset({"personal"})
    directory.members.return_value = [
        CalendarMember(
            name="A", slack_user_id="U2", label_id="a", calendar_emails={"same@example.com"}
        ),
        CalendarMember(
            name="B", slack_user_id="U3", label_id="b", calendar_emails={"SAME@example.com"}
        ),
    ]
    service = MemberMappingService(member_directory=directory)
    with pytest.raises(AgentError) as caught:
        service.participant_users("personal", [harness.calendar.event("personal", "event")])
    assert caught.value.code == "participant_mapping_conflict"
