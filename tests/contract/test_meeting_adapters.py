import json
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError
from slack_sdk.errors import SlackApiError, SlackClientError

from modules.interaction.application.services.meetings import announcement_text
from modules.interaction.infrastructure.messaging.slack_announcements import SlackAnnouncementSender
from modules.planning.public import MeetingAnnouncement
from modules.scheduling.domain.models import EventLabel
from modules.scheduling.infrastructure.external_services.google_calendar import (
    GoogleCalendarRepository,
)
from modules.scheduling.public import UpdateEvent
from shared.errors import AgentError, UncertainWrite
from shared.infrastructure.http import JsonHttp
from tests.support import draft


@pytest.mark.parametrize("guests", [(), ("n@example.com", "a@example.com")])
def test_google_creation_notifies_guests_once_and_verifies_readback(guests):
    stored = {}
    posts = []

    def respond(request):
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            assert request.url.params["sendUpdates"] == ("all" if guests else "none")
            if guests:
                assert body["attendees"] == [{"email": email} for email in guests]
            else:
                assert "attendees" not in body
            assert "eventLabelId" not in body
            stored.update(body | {"etag": "v1", "htmlLink": "https://calendar.google.com/test"})
            return httpx.Response(200, json=stored)
        assert request.method == "GET"
        return httpx.Response(200, json=stored) if stored else httpx.Response(404)

    client = httpx.Client(base_url="https://calendar.test/", transport=httpx.MockTransport(respond))
    repository = GoogleCalendarRepository(JsonHttp(client), {"personal": "calendar"})
    event_draft = draft().model_copy(update={"guest_emails": guests})
    first = repository.create("personal", event_draft, "job")
    assert first.guest_emails == frozenset(guests)
    second = repository.create("personal", event_draft, "job")
    assert second.id == first.id
    assert len(posts) == 1


def test_google_missing_invitee_in_readback_is_uncertain():
    stored = {}

    def respond(request):
        if request.method == "POST":
            stored.update(json.loads(request.content))
            stored["attendees"] = []
            return httpx.Response(200, json=stored)
        return httpx.Response(200, json=stored) if stored else httpx.Response(404)

    repository = GoogleCalendarRepository(
        JsonHttp(
            httpx.Client(base_url="https://calendar.test/", transport=httpx.MockTransport(respond))
        ),
        {"personal": "calendar"},
    )
    with pytest.raises(UncertainWrite):
        repository.create(
            "personal", draft().model_copy(update={"guest_emails": ("n@example.com",)}), "job"
        )


@pytest.mark.parametrize("returned_label", ["kaggle", "unnamed"])
def test_creation_checks_exact_label_after_google_write(returned_label):
    stored = {}
    writes = []
    label = EventLabel(id="kaggle", name="Kaggle", background_color="#7cb342")

    def respond(request):
        if request.url.path == "/calendars/calendar":
            return httpx.Response(
                200,
                json={
                    "labelProperties": {
                        "eventLabels": [
                            {"id": "kaggle", "name": "Kaggle", "backgroundColor": "#7cb342"},
                            {"id": "unnamed", "backgroundColor": "#7cb342"},
                        ]
                    }
                },
            )
        assert request.url.params["eventLabelVersion"] == "1"
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["eventLabelId"] == "kaggle" and "colorId" not in body
            writes.append(body)
            stored.update(body | {"eventLabelId": returned_label})
        return httpx.Response(200, json=stored) if stored else httpx.Response(404)

    with httpx.Client(
        base_url="https://calendar.test/", transport=httpx.MockTransport(respond)
    ) as client:
        repository = GoogleCalendarRepository(JsonHttp(client), {"personal": "calendar"})
        event_draft = draft().model_copy(update={"label": label})
        if returned_label == "kaggle":
            assert repository.create("personal", event_draft, "job").label == label
        else:
            with pytest.raises(UncertainWrite):
                repository.create("personal", event_draft, "job")
    assert len(writes) == 1


def test_existing_event_update_cannot_receive_guest_modifications():
    with pytest.raises(ValidationError):
        UpdateEvent(
            calendar_alias="personal",
            event_id="event",
            draft=draft().model_copy(update={"guest_emails": ("n@example.com",)}),
        )


def sender():
    client = Mock()
    client.conversations_info.return_value = {
        "channel": {"id": "C1", "name": "general", "is_member": True}
    }
    client.chat_postMessage.return_value = {"ts": "1000.000001"}
    return SlackAnnouncementSender(client, frozenset({"C1", "D1"}), "UBOT"), client


def test_announcement_channels_only_include_allowed_joined_channels():
    adapter, client = sender()
    assert adapter.channels() == [{"id": "C1", "name": "general"}]
    client.conversations_info.assert_called_once_with(channel="C1")
    client.conversations_info.return_value["channel"]["is_member"] = False
    assert adapter.channels() == []
    with pytest.raises(AgentError):
        adapter.send("COTHER", "text", "job")
    with pytest.raises(AgentError):
        adapter.send("D1", "text", "job")
    client.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("mention", [False, True])
def test_only_approved_flag_can_generate_channel_mention(mention):
    adapter, client = sender()
    announcement = MeetingAnnouncement(
        channel_id="C1",
        text="進捗確認 <!channel> <!here> <@U2> & @channel",
        mention_channel=mention,
    )
    text = announcement_text(announcement)
    assert text.count("<!channel>") == int(mention)
    assert "<!here>" not in text and "<@U2>" not in text
    adapter.send("C1", text, "job")
    args = client.chat_postMessage.call_args.kwargs
    assert args["text"] == text and args["parse"] == "none" and not args["link_names"]
    assert "thread_ts" not in args  # 周知はチャンネルの新規投稿。
    assert args["metadata"]["event_type"] == "kaggle_agent_meeting"


@pytest.mark.parametrize("error", [SlackClientError("offline"), OSError("offline")])
def test_uncertain_slack_post_never_retries(error):
    adapter, client = sender()
    client.chat_postMessage.side_effect = error
    with pytest.raises(UncertainWrite):
        adapter.send("C1", "text", "job")
    assert client.chat_postMessage.call_count == 1


def test_definite_slack_rejection_is_reported():
    adapter, client = sender()
    response = Mock(status_code=403)
    response.get.return_value = "not_in_channel"
    client.chat_postMessage.side_effect = SlackApiError("denied", response)
    with pytest.raises(AgentError) as caught:
        adapter.send("C1", "text", "job")
    assert caught.value.code == "announcement_rejected"
    assert client.chat_postMessage.call_count == 1


def test_reconcile_requires_matching_bot_metadata_and_body_across_pages():
    adapter, client = sender()
    adapter.send("C1", "text", "job")
    metadata = client.chat_postMessage.call_args.kwargs["metadata"]
    correct = {"user": "UBOT", "metadata": metadata, "text": "text", "ts": "1000.000001"}
    client.conversations_history.side_effect = [
        {
            "messages": [correct | {"user": "UOTHER"}, correct | {"text": "modified"}],
            "response_metadata": {"next_cursor": "next"},
        },
        {"messages": [correct]},
    ]
    assert adapter.find("C1", "text", "job") == "1000.000001"
    assert client.conversations_history.call_count == 2
    client.conversations_history.reset_mock(side_effect=True)
    client.conversations_history.return_value = {"messages": [correct | {"metadata": {}}]}
    assert adapter.find("C1", "text", "job") is None
    assert client.chat_postMessage.call_count == 1
