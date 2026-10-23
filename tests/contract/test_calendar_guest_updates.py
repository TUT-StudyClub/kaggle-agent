import json
from copy import deepcopy

import httpx
import pytest

from modules.interaction.composition import build_agent
from modules.interaction.domain.models import JobStatus
from modules.knowledge.composition import compose_knowledge
from modules.planning.public import Plan
from modules.scheduling.composition import compose_scheduling
from modules.scheduling.infrastructure.external_services.google_calendar import (
    GoogleCalendarRepository,
)
from modules.scheduling.public import UpdateEvent
from shared.infrastructure.http import JsonHttp
from tests.conftest import Harness
from tests.support import draft


def guest_event(**overrides) -> dict:
    return {
        "id": "meeting",
        "summary": "キックオフ",
        "start": {"dateTime": "2026-10-23T12:00:00+09:00"},
        "end": {"dateTime": "2026-10-23T12:45:00+09:00"},
        "etag": '"v1"',
        "organizer": {"self": True, "email": "calendar@example.com"},
        "creator": {"email": "host@example.com"},
        "attendees": [
            {"email": "host@example.com", "responseStatus": "accepted"},
            {"email": "guest@example.com", "responseStatus": "tentative", "optional": True},
            {"email": "room@example.com", "responseStatus": "accepted", "resource": True},
        ],
        "eventLabelId": "team",
        "description": "事前資料を確認する",
        "location": "会議室",
        "conferenceData": {"conferenceId": "existing-meeting"},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]},
        **overrides,
    }


def labels() -> dict:
    return {"labelProperties": {"eventLabels": [{"id": "team", "name": "チーム"}]}}


@pytest.mark.parametrize(
    "overrides,editable",
    [
        ({}, True),
        ({"attendees": []}, True),
        ({"organizer": {"self": False}}, False),
        ({"organizer": {}}, False),
        ({"locked": True}, False),
        ({"recurringEventId": "series"}, False),
        ({"recurrence": ["RRULE:FREQ=WEEKLY"]}, False),
        ({"eventType": "focusTime"}, False),
    ],
)
def test_guest_presence_does_not_bypass_other_editing_restrictions(overrides, editable):
    def handler(request):
        assert request.method == "GET"
        payload = guest_event(**overrides) if "/events/" in request.url.path else labels()
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        repo = GoogleCalendarRepository(JsonHttp(client), {"personal": "calendar-id"})
        assert repo.event("personal", "meeting").editable is editable


@pytest.mark.parametrize(
    "change,error_code",
    [
        ("none", None),
        ("event", "conflict"),
        ("organizer", "forbidden_event"),
        ("calendar", "forbidden_calendar"),
        ("during_patch", "conflict"),
    ],
)
def test_guest_event_update_requires_approval_and_preserves_invitation_data(
    harness: Harness, change, error_code
):
    raw = guest_event()
    before = deepcopy(raw)
    role = "owner"
    patches: list[httpx.Request] = []

    def handler(request):
        if request.url.path == "/users/me/calendarList/calendar-id":
            assert request.method == "GET"
            return httpx.Response(200, json={"accessRole": role})
        if request.url.path == "/calendars/calendar-id":
            assert request.method == "GET"
            return httpx.Response(200, json=labels())
        assert request.url.path == "/calendars/calendar-id/events/meeting"
        if request.method == "PATCH":
            patches.append(request)
            assert request.headers["If-Match"] == before["etag"]
            assert request.url.params["sendUpdates"] == "none"
            assert request.url.params["eventLabelVersion"] == "1"
            if change == "during_patch":
                return httpx.Response(412)
            body = json.loads(request.content)
            # ゲスト配列を含めると置換されるため、変更対象だけを送信する。
            assert set(body) == {"summary", "start", "end", "extendedProperties"}
            raw.update(body)
            raw["etag"] = '"v2"'
        else:
            assert request.method == "GET"
        return httpx.Response(200, json=raw)

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        repo = GoogleCalendarRepository(JsonHttp(client), {"personal": "calendar-id"})
        harness.agent = build_agent(
            harness.policy,
            harness.jobs,
            harness.planner,
            harness.messenger,
            compose_knowledge(harness.knowledge),
            compose_scheduling(repo, {"personal"}),
            harness.skillbook,
        )
        harness.planner.steps = [
            Plan(action=UpdateEvent(calendar_alias="personal", event_id="meeting", draft=draft()))
        ]
        job_id = harness.submit("ゲスト付きの予定を変更して")
        harness.agent.run_once()
        job = harness.jobs.get(job_id)
        assert job and job.status == JobStatus.AWAITING_APPROVAL
        assert not patches and raw == before
        assert job.plan_json
        pending = Plan.model_validate_json(job.plan_json).action
        assert isinstance(pending, UpdateEvent) and pending.expected_etag == before["etag"]

        if change == "event":
            raw["etag"] = '"changed"'
        elif change == "organizer":
            raw["organizer"] = {"self": False}
        elif change == "calendar":
            role = "writer"
        harness.approve(job_id)
        harness.agent.run_once()
        job = harness.jobs.get(job_id)
        assert job
        if error_code:
            assert job.error_code == error_code
            assert len(patches) == (1 if change == "during_patch" else 0)
            assert raw["start"] == before["start"] and raw["end"] == before["end"]
        else:
            assert job.status == JobStatus.SUCCEEDED and len(patches) == 1
            assert raw["summary"] == draft().title
            assert raw["start"]["dateTime"] == draft().period.start.isoformat()
            assert raw["end"]["dateTime"] == draft().period.end.isoformat()
            for field in (
                "attendees",
                "organizer",
                "creator",
                "eventLabelId",
                "description",
                "location",
                "conferenceData",
                "reminders",
            ):
                assert raw[field] == before[field]
            assert raw["extendedProperties"]["private"]["agentKey"] == job_id
            harness.approve(job_id)
            assert not harness.agent.run_once()
            assert len(patches) == 1
