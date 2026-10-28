import json

import httpx
import pytest

from modules.scheduling.infrastructure.external_services.google_calendar import (
    GoogleCalendarRepository,
)
from shared.errors import UncertainWrite
from shared.infrastructure.http import JsonHttp
from tests.support import draft


@pytest.mark.parametrize("ready", [True, False])
def test_meet_creation_waits_for_link_and_never_recreates_event(monkeypatch, ready):
    monkeypatch.setattr(
        "modules.scheduling.infrastructure.external_services.google_calendar.time.sleep",
        lambda _: None,
    )
    event = {}
    writes = []
    reads = 0

    def handler(request):
        nonlocal reads
        if request.method == "POST":
            assert request.url.params["conferenceDataVersion"] == "1"
            assert request.url.params["sendUpdates"] == "all"
            event.update(json.loads(request.content))
            assert event["conferenceData"]["createRequest"]["conferenceSolutionKey"] == {
                "type": "hangoutsMeet"
            }
            writes.append(event["id"])
            return httpx.Response(200, json=event)
        if not event:
            return httpx.Response(404)
        reads += 1
        response = dict(event, etag="v1")
        if ready and reads >= 2:
            response["conferenceData"] = {
                "conferenceSolution": {"key": {"type": "hangoutsMeet"}},
                "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/test"}],
            }
        return httpx.Response(200, json=response)

    event_draft = draft().model_copy(
        update={"create_google_meet": True, "guest_emails": ("guest@example.com",)}
    )
    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        repo = GoogleCalendarRepository(JsonHttp(client), {"personal": "calendar"})
        if ready:
            result = repo.create("personal", event_draft, "job")
            assert result.meet_url == "https://meet.google.com/test"
            assert repo.create("personal", event_draft, "job").meet_url == result.meet_url
        else:
            with pytest.raises(UncertainWrite):
                repo.create("personal", event_draft, "job")
            with pytest.raises(UncertainWrite):
                repo.create("personal", event_draft, "job")
        assert len(writes) == 1
