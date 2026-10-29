import sqlite3
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import Mock, create_autospec

import httpx
import pytest
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

import composition
from config import Settings
from modules.interaction.application.use_cases.remind_schedule import RemindScheduleUseCase
from modules.interaction.infrastructure.messaging.slack_reminders import SlackReminderSender
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.interaction.public import DailyScheduleReminder
from modules.scheduling.composition import compose_scheduling
from modules.scheduling.domain.models import CalendarMember
from modules.scheduling.infrastructure.external_services.google_calendar import (
    GoogleCalendarRepository,
)
from shared.application.ports.clock import Clock
from shared.errors import AgentError
from shared.infrastructure.http import JsonHttp


class ReminderClock(Clock):
    value = "2026-10-23T10:00:00+09:00"

    def now(self) -> datetime:
        return datetime.fromisoformat(self.value)

    def monotonic(self) -> float:
        return self.timestamp()


def event(title="キックオフ", label="team", **changes):
    return {
        "id": title,
        "summary": title,
        "start": {"dateTime": "2026-10-23T19:00:00+09:00"},
        "end": {"dateTime": "2026-10-23T20:00:00+09:00"},
        "eventLabelId": label,
        "creator": {"email": "host@example.com"},
        "attendees": [{"email": "guest@example.com", "responseStatus": "needsAction"}],
        "htmlLink": "https://calendar.google.com/event/test",
        **changes,
    }


@pytest.fixture
def reminder(tmp_path):
    clock = ReminderClock()
    config = DailyScheduleReminder(calendar_alias="club", label_id="team", channel_id="C1")
    slack = create_autospec(WebClient, instance=True)
    slack.chat_postMessage.return_value = {"ok": True, "ts": "123.456"}
    slack.conversations_members.return_value = {"members": ["U1", "U2", "U3", "UBOT"]}
    slack.users_info.side_effect = lambda *, user: {
        "user": {"id": user, "is_bot": user == "UBOT", "deleted": False},
    }
    requests = []
    pages = [[event()]]

    def response(request):
        assert request.method == "GET"
        requests.append(request)
        if request.url.path.endswith("/events"):
            page = int(request.url.params.get("pageToken", "0"))
            data = {"items": pages[page]}
            if page + 1 < len(pages):
                data["nextPageToken"] = str(page + 1)
            return httpx.Response(200, json=data)
        return httpx.Response(
            200,
            json={
                "labelProperties": {
                    "eventLabels": [
                        {"id": "team", "name": "Kaggle", "backgroundColor": "#7cb342"},
                        {"id": "unnamed", "name": None, "backgroundColor": "#7cb342"},
                    ]
                },
            },
        )

    with httpx.Client(
        base_url="https://test.invalid/", transport=httpx.MockTransport(response)
    ) as client:
        directory = Mock()
        directory.calendar_aliases = frozenset({"club"})
        directory.members.return_value = [
            CalendarMember(
                name="招待者",
                slack_user_id="U1",
                label_id="a",
                calendar_emails={"host@example.com"},
            ),
            CalendarMember(
                name="ゲスト",
                slack_user_id="U2",
                label_id="b",
                calendar_emails={"guest@example.com", "alias@example.com"},
            ),
            CalendarMember(
                name="メンバー",
                slack_user_id="U3",
                label_id="c",
                calendar_emails={"third@example.com"},
            ),
        ]
        scheduling = compose_scheduling(
            GoogleCalendarRepository(JsonHttp(client), {"club": "calendar-id"}),
            set(),
            member_directory=directory,
        )
        path = tmp_path / "agent.sqlite3"

        def restart():
            return RemindScheduleUseCase(
                config,
                "T1",
                scheduling,
                SQLiteJobRepository(path),
                SlackReminderSender(slack, frozenset({"C1"})),
                clock,
            )

        yield restart(), restart, clock, slack, pages, requests, path


def test_daily_notification_uses_jst_exact_label_and_persists_after_restart(reminder):
    usecase, restart, clock, slack, pages, requests, path = reminder
    pages[:] = [
        [event("個人予定", "unnamed"), event("色だけ", None, colorId="10")],
        [
            event("キックオフ"),
            event("削除済み", status="cancelled"),
            event("終日イベント", start={"date": "2026-10-22"}, end={"date": "2026-10-24"}),
            event(
                "前夜から",
                start={"dateTime": "2026-10-22T23:00:00+09:00"},
                end={"dateTime": "2026-10-23T11:00:00+09:00"},
            ),
        ],
    ]
    clock.value = "2026-10-23T00:59:59+00:00"  # JST 09:59:59
    usecase.execute()
    assert not requests
    clock.value = "2026-10-23T01:00:00+00:00"
    usecase.execute()
    sent = slack.chat_postMessage.call_args.kwargs
    assert sent["channel"] == "C1" and "thread_ts" not in sent
    assert sent["text"].startswith("今日のチーム予定（10/23）")
    assert "19:00〜20:00 キックオフ" in sent["text"]
    assert "終日 終日イベント" in sent["text"]
    assert "10/22 23:00〜11:00 前夜から" in sent["text"]
    assert sent["text"].index("終日イベント") < sent["text"].index("キックオフ")
    assert all(title not in sent["text"] for title in ["個人予定", "色だけ", "削除済み"])
    assert "予定を開く" not in sent["text"]
    assert "https://calendar.google.com/event/test" not in sent["text"]
    params = requests[0].url.params
    assert params["timeMin"] == "2026-10-23T00:00:00+09:00"
    assert params["timeMax"] == "2026-10-24T00:00:00+09:00"
    assert params["singleEvents"] == "true"
    assert any(r.url.params.get("pageToken") == "1" for r in requests)
    reads = len(requests)
    restart().execute()
    assert len(requests) == reads
    slack.chat_postMessage.assert_called_once()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state,message_ts FROM schedule_reminders").fetchall() == [
            ("sent", "123.456")
        ]


def test_offline_at_ten_catches_up_same_day_and_runs_again_next_day(reminder):
    _, restart, clock, slack, _, requests, _ = reminder
    clock.value = "2026-10-23T22:00:00+09:00"
    restart().execute()
    clock.value = "2026-10-24T09:00:00+09:00"
    restart().execute()
    assert slack.chat_postMessage.call_count == 1
    clock.value = "2026-10-24T10:00:00+09:00"
    restart().execute()
    assert slack.chat_postMessage.call_count == 2
    assert requests[-2].url.params["timeMin"].startswith("2026-10-24")


def test_empty_day_sends_nothing_and_is_remembered(reminder):
    usecase, restart, _, slack, pages, requests, _ = reminder
    pages[:] = [[event("個人", "unnamed")]]
    usecase.execute()
    reads = len(requests)
    restart().execute()
    assert len(requests) == reads
    slack.chat_postMessage.assert_not_called()


def test_calendar_failure_retries_without_losing_the_day(reminder, monkeypatch):
    usecase, _, clock, slack, _, _, _ = reminder
    original = usecase.scheduling.events_for_label
    monkeypatch.setattr(
        usecase.scheduling, "events_for_label", Mock(side_effect=AgentError("read", ""))
    )
    usecase.execute()
    slack.chat_postMessage.assert_not_called()
    monkeypatch.setattr(usecase.scheduling, "events_for_label", original)
    usecase.execute()
    slack.chat_postMessage.assert_not_called()
    clock.value = "2026-10-23T10:01:00+09:00"
    usecase.execute()
    slack.chat_postMessage.assert_called_once()


def test_explicit_slack_rejection_can_retry(reminder):
    usecase, _, clock, slack, _, _, _ = reminder
    response = SlackResponse(
        client=slack,
        http_verb="POST",
        api_url="https://slack.test/",
        req_args={},
        data={"ok": False, "error": "ratelimited"},
        headers={"Retry-After": "1"},
        status_code=429,
    )
    slack.chat_postMessage.side_effect = SlackApiError("rejected", response)
    usecase.execute()
    slack.chat_postMessage.side_effect = None
    clock.value = "2026-10-23T10:01:00+09:00"
    usecase.execute()
    assert slack.chat_postMessage.call_count == 2


@pytest.mark.parametrize("failure", ["network", "server", "missing_ts", "disk_after_send"])
def test_uncertain_send_never_posts_twice_after_restart(reminder, failure, monkeypatch, caplog):
    usecase, restart, _, slack, _, _, _ = reminder
    if failure == "network":
        slack.chat_postMessage.side_effect = OSError("secret-must-not-appear")
    elif failure == "server":
        response = SlackResponse(
            client=slack,
            http_verb="POST",
            api_url="https://slack.test/",
            req_args={},
            data={"ok": False, "error": "internal_error"},
            headers={},
            status_code=500,
        )
        slack.chat_postMessage.side_effect = SlackApiError("secret-must-not-appear", response)
    elif failure == "missing_ts":
        slack.chat_postMessage.return_value = {"ok": True}
    else:
        monkeypatch.setattr(usecase.repository, "finish_reminder", Mock(side_effect=OSError()))
    usecase.execute()
    usecase.execute()
    restart().execute()
    slack.chat_postMessage.assert_called_once()
    assert "secret-must-not-appear" not in caplog.text
    assert "uncertain_write" in caplog.text


def test_atomic_daily_reservation_and_v2_migration_preserve_existing_jobs(reminder):
    _, _, clock, _, _, _, path = reminder
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO seen_events VALUES ('keep')")
        db.execute("DROP TABLE schedule_reminders")
        db.execute("PRAGMA user_version=2")
    first = SQLiteJobRepository(path)
    second = SQLiteJobRepository(path)
    assert first.reserve_reminder("same-day", clock.timestamp())
    assert not second.reserve_reminder("same-day", clock.timestamp())
    first.purge(clock.timestamp() - 86400)
    assert second.reminder_state("same-day") == "sending"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM seen_events").fetchall() == [("keep",)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


def test_calendar_title_cannot_inject_slack_mentions(reminder):
    usecase, _, _, slack, pages, _, _ = reminder
    pages[:] = [[event("<!channel> <@U123> & <https://evil.test|click>")]]
    usecase.execute()
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert "<!channel>" not in text and "<@U123>" not in text
    assert "&lt;!channel&gt;" in text


def test_root_composition_wires_reminder_and_identity_mapping_without_ai(
    tmp_path, monkeypatch, harness
):
    clock = ReminderClock()
    slack = create_autospec(WebClient, instance=True)
    slack.auth_test.return_value = {"team_id": "T1", "user_id": "UBOT"}
    slack.chat_postMessage.return_value = {"ok": True, "ts": "123.456"}
    slack.conversations_members.return_value = {"members": ["U1", "U2"]}
    slack.users_info.side_effect = lambda *, user: {"user": {"id": user, "is_bot": False}}
    planning = Mock(skillbook=harness.skillbook)
    monkeypatch.setattr(composition, "build_clock", lambda: clock)
    monkeypatch.setattr(composition, "build_planning", lambda *args, **kwargs: planning)
    monkeypatch.setattr(composition, "WebClient", lambda **kwargs: slack)
    monkeypatch.setattr(composition, "App", Mock())
    monkeypatch.setattr(composition.Credentials, "from_authorized_user_file", Mock())
    requests = []

    def response(request):
        requests.append(request)
        if request.url.host == "api.notion.com":
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "properties": {
                            "Name": {"type": "title"},
                            "Slack User ID": {"type": "rich_text"},
                            "Calendar Label ID": {"type": "rich_text"},
                            "Calendar Emails": {"type": "rich_text"},
                        }
                    },
                )
            rows = []
            for name, user, label, email in [
                ("招待者", "U1", "a", "host@example.com"),
                ("ゲスト", "U2", "b", "guest@example.com"),
            ]:
                props = {}
                for column, kind, value in [
                    ("Name", "title", name),
                    ("Slack User ID", "rich_text", user),
                    ("Calendar Label ID", "rich_text", label),
                    ("Calendar Emails", "rich_text", email),
                ]:
                    props[column] = {"type": kind, kind: [{"plain_text": value}]}
                rows.append({"properties": props})
            return httpx.Response(200, json={"results": rows, "has_more": False})
        assert request.url.host == "www.googleapis.com" and request.method == "GET"
        return httpx.Response(
            200, json={"items": [event()]} if request.url.path.endswith("/events") else {}
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        composition.httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(response)),
    )
    token_file = tmp_path / "google.json"
    token_file.write_text("{}")
    settings = Settings(
        team_id="T1",
        user_ids={"U1"},
        channel_ids={"C1"},
        calendars={
            "club": {
                "id": "calendar",
                "member_data_source_id": "00000000-0000-0000-0000-000000000001",
            }
        },
        database=tmp_path / "agent.sqlite3",
        google_token_file=token_file,
        slack_bot_token="test",
        slack_app_token="test",
        notion_token="test",
        daily_schedule_reminder=DailyScheduleReminder(
            calendar_alias="club", label_id="team", channel_id="C1"
        ),
    )
    with ExitStack() as stack:
        _, agent = composition.build(settings, stack)
        agent.remind_schedule()
    slack.chat_postMessage.assert_called_once()
    assert "<!channel>" in slack.chat_postMessage.call_args.kwargs["text"]
    planning.plan.assert_not_called()
    assert requests


def test_read_crossing_midnight_does_not_post_yesterdays_schedule(reminder, monkeypatch):
    usecase, _, clock, slack, _, _, _ = reminder
    clock.value = "2026-10-23T23:59:59+09:00"
    original = usecase.scheduling.events_for_label

    def crosses_midnight(*args):
        events = original(*args)
        clock.value = "2026-10-24T00:00:01+09:00"
        return events

    monkeypatch.setattr(usecase.scheduling, "events_for_label", crosses_midnight)
    usecase.execute()
    slack.chat_postMessage.assert_not_called()


def test_sender_rejects_channel_outside_allowlist_before_posting(reminder):
    _, _, _, slack, _, _, _ = reminder
    with pytest.raises(AgentError, match="許可範囲外"):
        SlackReminderSender(slack, frozenset({"C1"})).send("COTHER", "予定")
    slack.chat_postMessage.assert_not_called()


def test_creator_and_all_invited_guests_mention_channel(reminder):
    usecase, _, _, slack, pages, _, _ = reminder
    pages[:] = [
        [
            event(
                attendees=[
                    {"email": "host@example.com", "responseStatus": "accepted"},
                    {"email": "guest@example.com", "responseStatus": "needsAction"},
                    {"email": "third@example.com", "responseStatus": "tentative", "optional": True},
                    {"email": "room@example.com", "resource": True},
                ]
            )
        ]
    ]
    usecase.execute()
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert text.count("<!channel>") == 1
    assert "参加者: <!channel>" in text
    assert "<@U1>" not in text


def test_partial_events_mention_each_events_participants_without_union_broadcast(reminder):
    usecase, _, _, slack, pages, _, _ = reminder
    pages[:] = [
        [event("打合せA"), event("打合せB", creator={"email": "third@example.com"}, attendees=[])]
    ]
    usecase.execute()
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert "<!channel>" not in text
    first, second = text.split("打合せB")
    assert "参加者: <@U1> <@U2>" in first
    assert "参加者: <@U3>" in second
    assert "<@UBOT>" not in text


def test_explicit_alias_and_case_normalization_deduplicate_participants(reminder):
    usecase, _, _, slack, pages, _, _ = reminder
    pages[:] = [
        [
            event(
                creator={"email": "HOST@EXAMPLE.COM"},
                attendees=[
                    {"email": "alias@example.com"},
                    {"email": "Guest@Example.com"},
                    {"email": "host@example.com"},
                ],
            )
        ]
    ]
    usecase.execute()
    text = slack.chat_postMessage.call_args.kwargs["text"]
    assert text.count("<@U1>") == 1 and text.count("<@U2>") == 1
    assert "<!channel>" not in text


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"creator": {}}, "calendar_participants_incomplete"),
        ({"attendeesOmitted": True}, "calendar_participants_incomplete"),
        ({"attendees": [{}]}, "calendar_participants_incomplete"),
        ({"attendees": [{"email": "unknown@example.com"}]}, "participant_not_found"),
    ],
)
def test_unknown_or_incomplete_identity_stops_before_reserving_or_sending(
    reminder, changes, code, caplog
):
    usecase, _, _, slack, pages, _, path = reminder
    pages[:] = [[event(**changes)]]
    usecase.execute()
    slack.chat_postMessage.assert_not_called()
    assert code in caplog.text
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM schedule_reminders").fetchall() == []


def test_channel_membership_read_failure_does_not_post_or_reserve(reminder, caplog):
    usecase, _, _, slack, _, _, path = reminder
    slack.conversations_members.side_effect = OSError("private-error")
    usecase.execute()
    slack.chat_postMessage.assert_not_called()
    assert "slack_members_network" in caplog.text and "private-error" not in caplog.text
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT * FROM schedule_reminders").fetchall()


def test_membership_read_crossing_midnight_does_not_send_previous_day(reminder):
    usecase, _, clock, slack, _, _, _ = reminder
    clock.value = "2026-10-23T23:59:59+09:00"

    def profile(*, user):
        clock.value = "2026-10-24T00:00:01+09:00"
        return {"user": {"id": user, "is_bot": user == "UBOT"}}

    slack.users_info.side_effect = profile
    usecase.execute()
    slack.chat_postMessage.assert_not_called()
