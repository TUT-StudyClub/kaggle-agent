import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from config import Settings
from modules.planning.public import Plan
from modules.scheduling.composition import compose_scheduling
from modules.scheduling.domain.models import Event, EventLabel
from modules.scheduling.infrastructure.external_services.google_calendar import (
    GoogleCalendarRepository,
)
from modules.scheduling.public import CalendarConfig, ListCalendars, ListEvents
from shared.errors import AgentError
from shared.infrastructure.http import JsonHttp
from tests.conftest import Harness
from tests.support import period


def raw_event(event_id: str = "event1", **extra) -> dict:
    return {
        "id": event_id,
        "summary": "打合せ",
        "start": {"dateTime": "2026-10-23T10:00:00+09:00"},
        "end": {"dateTime": "2026-10-23T11:00:00+09:00"},
        "htmlLink": "https://calendar.google.com/event/test",
        **extra,
    }


def label_metadata() -> dict:
    return {
        "labelProperties": {
            "eventLabels": [{"id": "label-a", "name": "田中", "backgroundColor": "#039be5"}]
        }
    }


@pytest.mark.parametrize("single", [False, True])
def test_event_label_name_and_color_reach_the_public_result(single):
    paths = []

    def handler(request):
        assert request.method == "GET"
        paths.append(request.url.path)
        if request.url.path.endswith("/events/event1"):
            return httpx.Response(200, json=raw_event(eventLabelId="label-a", colorId="7"))
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json={"items": [raw_event(eventLabelId="label-a", colorId="7")]},
            )
        return httpx.Response(200, json=label_metadata())

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        repo = GoogleCalendarRepository(JsonHttp(client), {"personal": "calendar-id"})
        event = repo.event("personal", "event1") if single else repo.events("personal", period())[0]
    assert event.label_id == "label-a"
    assert event.label == EventLabel(id="label-a", name="田中", background_color="#039be5")
    assert event.color_id == "7"
    assert not event.editable  # 人名ラベルは所有権を意味しない。
    assert len(paths) == 2


@pytest.mark.parametrize("location", ["東京国際フォーラム", ""])
def test_event_location_reaches_read_result_without_fabricating_missing_location(location):
    with httpx.Client(
        base_url="https://test.invalid/",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"items": [raw_event(location=location)]})
        ),
    ) as client:
        repository = GoogleCalendarRepository(JsonHttp(client), {"personal": "calendar-id"})
        service = compose_scheduling(repository, set())
        result = service.execute(ListEvents(calendar_alias="personal", period=period()), "test")
    events = result["events"]
    assert isinstance(events, list)
    assert events[0]["location"] == location


def test_paginated_events_fetch_labels_once_and_keep_cancelled_events_out():
    metadata_reads = []

    def handler(request):
        if request.url.path.endswith("/events"):
            if "pageToken" in request.url.params:
                return httpx.Response(
                    200, json={"items": [raw_event("event2", eventLabelId="label-a")]}
                )
            return httpx.Response(
                200,
                json={
                    "items": [raw_event(eventLabelId="label-a"), {"status": "cancelled"}],
                    "nextPageToken": "next",
                },
            )
        metadata_reads.append(request)
        return httpx.Response(200, json=label_metadata())

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        events = GoogleCalendarRepository(JsonHttp(client), {"personal": "id"}).events(
            "personal", period()
        )
    assert len(events) == 2 and all(e.label and e.label.name == "田中" for e in events)
    assert len(metadata_reads) == 1


def test_color_only_and_unlabelled_events_do_not_guess_a_person_or_read_metadata():
    def handler(request):
        assert request.url.path.endswith("/events")
        return httpx.Response(
            200,
            json={"items": [raw_event(colorId="7"), raw_event("plain")]},
        )

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        events = GoogleCalendarRepository(JsonHttp(client), {"personal": "id"}).events(
            "personal", period()
        )
    assert events[0].color_id == "7"
    assert all(e.label is None and e.label_id is None for e in events)


def test_unknown_label_id_is_preserved_without_matching_by_color():
    def handler(request):
        payload = (
            raw_event(eventLabelId="unknown", colorId="7")
            if request.url.path.endswith("/events/event1")
            else label_metadata()
        )
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        event = GoogleCalendarRepository(JsonHttp(client), {"personal": "id"}).event(
            "personal", "event1"
        )
    assert event.label_id == "unknown" and event.label is None


def test_missing_metadata_permission_explains_reauthentication_without_exposing_body():
    def handler(request):
        if request.url.path.endswith("/events/event1"):
            return httpx.Response(200, json=raw_event(eventLabelId="label-a"))
        return httpx.Response(403, text="private-google-error")

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(AgentError, match="task auth:google") as caught:
            GoogleCalendarRepository(JsonHttp(client), {"personal": "id"}).event(
                "personal", "event1"
            )
    assert caught.value.code == "calendar_labels_permission"
    assert "private-google-error" not in str(caught.value)


def test_calendar_list_exposes_existing_labels_and_configured_slack_mapping():
    def handler(request):
        assert request.method == "GET"
        payload = (
            {"accessRole": "reader", "summary": "共有予定"}
            if "/calendarList/" in request.url.path
            else label_metadata()
        )
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        service = compose_scheduling(
            GoogleCalendarRepository(JsonHttp(client), {"shared": "id"}),
            set(),
            {"shared": {"label-a": "U2"}},
        )
        result = service.execute(ListCalendars(), "read")
    calendars = result["calendars"]
    assert isinstance(calendars, list)
    calendar = calendars[0]
    assert calendar["labels"][0] == {
        "id": "label-a",
        "name": "田中",
        "background_color": "#039be5",
        "slack_user_id": "U2",
    }
    assert calendar["role"] == "reader" and not calendar["owned"]


def test_label_filter_and_slack_mapping_use_id_and_calendar_not_shared_name(harness: Harness):
    base = harness.calendar.event("personal", "one")
    for event_id, alias, label_id in (
        ("one", "personal", "label-a"),
        ("two", "personal", "label-b"),
        ("three", "personal", None),
    ):
        harness.calendar.stored[event_id] = base.model_copy(
            update={
                "id": event_id,
                "calendar_alias": alias,
                "label_id": label_id,
                "label": EventLabel(id=label_id, name="田中") if label_id else None,
            }
        )
    service = compose_scheduling(
        harness.calendar,
        {"personal"},
        {"personal": {"label-a": "U2"}, "other": {"label-b": "U3"}},
    )
    result = service.execute(ListEvents(calendar_alias="personal", period=period()), "read")
    events = result["events"]
    assert isinstance(events, list)
    assert [e["slack_user_id"] for e in events] == ["U2", None, None]
    filtered = service.execute(
        ListEvents(calendar_alias="personal", period=period(), label_id="label-a"), "read"
    )
    filtered_events = filtered["events"]
    assert isinstance(filtered_events, list)
    assert [e["id"] for e in filtered_events] == ["one"]
    assert harness.calendar.writes == 0


def test_mention_lookup_passes_requester_mapping_and_label_to_planner(harness: Harness):
    mapping = {"label-a": "U2"}
    harness.configure(
        policy=harness.policy.model_copy(
            update={"calendars": {"personal": CalendarConfig(id="id", label_users=mapping)}}
        ),
        label_users={"personal": mapping},
    )
    harness.calendar.stored["event1"] = Event(
        id="event1",
        calendar_alias="personal",
        title="打合せ",
        start=period().start.isoformat(),
        end=period().end.isoformat(),
        url="https://calendar.google.com/event/test",
        etag="v1",
        label_id="label-a",
        label=EventLabel(id="label-a", name="田中", background_color="#039be5"),
    )
    harness.planner.steps = [
        Plan(action=ListEvents(calendar_alias="personal", period=period(), label_id="label-a")),
        Plan.model_validate({"action": {"kind": "reply", "text": "田中さんの予定は打合せです。"}}),
    ]
    job_id = harness.submit("<@U2>の予定を教えて")
    harness.agent.run_once()
    request = harness.planner.requests[-1]
    assert request.instruction == "<@U2>の予定を教えて"
    assert request.resources["requester_slack_user_id"] == "U1"
    calendars = request.resources["calendars"]
    assert isinstance(calendars, dict)
    assert calendars["personal"]["label_users"] == mapping
    events = request.observations[-1]["events"]
    assert isinstance(events, list)
    event = events[0]
    assert event["label"]["name"] == "田中" and event["slack_user_id"] == "U2"
    job = harness.jobs.get(job_id)
    assert job and job.result_json
    context = json.loads(job.result_json)
    assert context["events"][0]["label"]["name"] == "田中"
    assert harness.calendar.writes == 0


def test_settings_load_calendar_scoped_label_users_without_adding_bot_access(
    tmp_path: Path, monkeypatch
):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "TEAM_ID=T1\nUSER_IDS='[\"U1\"]'\nCHANNEL_IDS='[\"D1\"]'\n"
        'CALENDARS=\'{"shared":{"id":"shared-calendar","label_users":{"label-a":"U2"}}}\'\n',
        encoding="utf-8",
    )
    config = Settings.load(path)
    assert config.calendars["shared"].label_users == {"label-a": "U2"}
    assert config.user_ids == frozenset({"U1"})
    assert not config.calendars["shared"].writable


@pytest.mark.parametrize("mapping", [{"": "U2"}, {"label-a": ""}, {"label-a": "<@U2>"}])
def test_label_mapping_rejects_empty_ids_and_mention_markup(mapping):
    with pytest.raises(ValidationError):
        CalendarConfig(id="id", label_users=mapping)
