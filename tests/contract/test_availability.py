from unittest.mock import Mock

import pytest

from modules.scheduling.composition import compose_scheduling
from modules.scheduling.domain.models import CalendarMember, EventLabel
from modules.scheduling.domain.repositories import MemberDirectory
from modules.scheduling.public import FindFree, TimeRange
from shared.errors import AgentError


def window(start="2026-10-30T19:00:00+09:00", end="2026-10-30T20:00:00+09:00"):
    return TimeRange(start=start, end=end)


@pytest.fixture
def team(harness, monkeypatch):
    directory = Mock(spec=MemberDirectory)
    directory.calendar_aliases = frozenset({"personal"})
    directory.members.return_value = [
        CalendarMember(
            name="山田", slack_user_id="U1", label_id="member-a", calendar_emails={"a@example.com"}
        ),
        CalendarMember(
            name="秋山", slack_user_id="U2", label_id="member-b", calendar_emails={"b@example.com"}
        ),
    ]
    calendar = harness.calendar.calendars()[0].model_copy(
        update={
            "labels": [EventLabel(id="member-a"), EventLabel(id="member-b"), EventLabel(id="other")]
        }
    )
    monkeypatch.setattr(harness.calendar, "calendars", lambda: [calendar])
    monkeypatch.setattr(
        harness.calendar, "busy", Mock(side_effect=AssertionError("全体Busyを使ってはいけない"))
    )
    harness.configure(member_directory=directory)
    service = compose_scheduling(harness.calendar, set(), member_directory=directory)
    return harness, directory, service


def add_event(harness, key, start, end, label="member-a", **extra):
    event = harness.calendar.event("personal", key).model_copy(
        update={"start": start, "end": end, "label_id": label, "title": key, **extra}
    )
    harness.calendar.stored[key] = event


def find(service, period=None, **extra):
    return service.execute(
        FindFree(calendar_aliases=["personal"], period=period or window(), minutes=60, **extra),
        "availability-test",
    )


@pytest.mark.parametrize("outsider_label", [None, "other"])
def test_nonmember_piano_does_not_block_or_leak_into_team_availability(team, outsider_label):
    harness, directory, service = team
    add_event(harness, "インターン", "2026-10-30T18:30:00+09:00", "2026-10-30T19:00:00+09:00")
    add_event(
        harness,
        "ピアノ",
        "2026-10-30T19:00:00+09:00",
        "2026-10-30T21:00:00+09:00",
        label=outsider_label,
        color_id="7",
    )
    result = find(service)
    assert result["free"] == [window().model_dump(mode="json")]
    assert [m["slack_user_id"] for m in result["availability"]["members"]] == ["U1", "U2"]
    assert "ピアノ" not in str(result)
    assert directory.members.call_count == 1
    assert harness.calendar.writes == 0


def test_half_open_intervals_merge_members_and_clip_to_search_window(team):
    harness, _, service = team
    add_event(harness, "work", "2026-10-30T18:00:00+09:00", "2026-10-30T19:15:00+09:00")
    add_event(
        harness,
        "class",
        "2026-10-30T19:00:00+09:00",
        "2026-10-30T20:00:00+09:00",
        label="member-b",
    )
    add_event(harness, "next", "2026-10-30T21:00:00+09:00", "2026-10-30T22:00:00+09:00")
    result = find(service, window(end="2026-10-30T22:00:00+09:00"))
    assert result["free"] == [
        window("2026-10-30T20:00:00+09:00", "2026-10-30T21:00:00+09:00").model_dump(mode="json")
    ]


def test_requested_member_and_latest_mapping_are_resolved_each_time(team):
    harness, directory, service = team
    add_event(
        harness, "busy", window().start.isoformat(), window().end.isoformat(), label="member-b"
    )
    assert find(service, slack_user_ids=["U1"])["free"]
    directory.members.return_value = [
        CalendarMember(name="山田", slack_user_id="U1", label_id="member-b")
    ]
    assert find(service, slack_user_ids=["U1"])["free"] == []
    assert directory.members.call_count == 2


@pytest.mark.parametrize("members", [[], None])
def test_empty_or_failed_directory_never_falls_back_to_whole_calendar(team, members):
    _, directory, service = team
    if members is None:
        directory.members.side_effect = AgentError("directory_down", "取得失敗")
    else:
        directory.members.return_value = members
    with pytest.raises(AgentError):
        find(service)


def test_missing_member_or_label_stops_without_guessing(team):
    _, directory, service = team
    with pytest.raises(AgentError, match="Member DBにありません"):
        find(service, slack_user_ids=["U99"])
    directory.members.return_value = [
        CalendarMember(name="山田", slack_user_id="U1", label_id="missing")
    ]
    with pytest.raises(AgentError, match="ラベルID"):
        find(service)


def test_all_day_exclusive_end_and_transparent_events(team):
    harness, _, service = team
    add_event(harness, "holiday", "2026-10-30", "2026-10-31")
    add_event(
        harness,
        "free-marker",
        "2026-10-31T09:00:00+09:00",
        "2026-10-31T22:00:00+09:00",
        label="member-b",
        blocks_time=False,
    )
    result = find(
        service,
        window("2026-10-30T00:00:00+09:00", "2026-11-01T00:00:00+09:00"),
        daily_start_minute=19 * 60,
        daily_end_minute=20 * 60,
    )
    assert result["free"] == [
        window("2026-10-31T19:00:00+09:00", "2026-10-31T20:00:00+09:00").model_dump(mode="json")
    ]


def test_utc_times_are_compared_in_japan_time_and_invalid_member_times_fail(team):
    harness, _, service = team
    add_event(harness, "utc", "2026-10-30T10:00:00Z", "2026-10-30T11:00:00Z")
    assert find(service)["free"] == []
    add_event(harness, "invalid", "2026-10-30T19:00:00", "2026-10-30T20:00:00")
    with pytest.raises(AgentError, match="開始・終了日時"):
        find(service)


def test_without_directory_whole_calendar_busy_still_uses_python(harness, monkeypatch):
    busy = Mock(return_value=[window()])
    monkeypatch.setattr(harness.calendar, "busy", busy)
    service = compose_scheduling(harness.calendar, set())
    result = find(service)
    assert result["free"] == []
    assert result["availability"]["scope"] == "calendars"
    busy.assert_called_once()
    with pytest.raises(AgentError, match="Member DB"):
        find(service, slack_user_ids=["U1"])


def test_agent_sends_calculated_availability_without_a_second_ai_interpretation(team):
    from modules.planning.public import Plan, Reply
    from modules.scheduling.public import ListCalendars

    harness, _, _ = team
    add_event(
        harness, "ピアノ", "2026-10-30T20:00:00+09:00", "2026-10-30T21:00:00+09:00", label=None
    )
    harness.planner.steps = [
        Plan(action=ListCalendars()),
        Plan(action=FindFree(calendar_aliases=["personal"], period=window(), minutes=60)),
        Plan(action=Reply(text="11月30日、ピアノは19時なので空いていないよ")),
    ]
    job_id = harness.submit("10/30の19〜20時、みんな空いてる？")
    harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert harness.jobs.get(job_id).status == "succeeded"
    assert len(harness.planner.requests) == 2
    reply = harness.messenger.sent[-1]
    assert "2026/10/30（金）19:00〜20:00" in reply
    assert "対象：山田・秋山" in reply
    assert "ピアノ" not in reply and "11月30日" not in reply


def test_guidance_requires_python_for_new_queries_and_corrections():
    from modules.planning.infrastructure.skills import load_skillbook

    prompt = load_skillbook().prompt()
    assert "空き時間・「この時間は空いている？」の確認は必ず`calendar.free`" in prompt
    assert "訂正・指摘への対応でも" in prompt
    assert "context.dialogue" in prompt


@pytest.fixture
def shared_meeting(team, monkeypatch):
    harness, directory, _ = team
    directory.members.return_value = [
        *directory.members.return_value,
        CalendarMember(
            name="中川", slack_user_id="U3", label_id="member-c", calendar_emails={"c@example.com"}
        ),
    ]
    calendar = harness.calendar.calendars()[0]
    monkeypatch.setattr(
        harness.calendar,
        "calendars",
        lambda: [
            calendar.model_copy(update={"labels": [*calendar.labels, EventLabel(id="member-c")]})
        ],
    )
    service = compose_scheduling(
        harness.calendar,
        set(),
        member_directory=directory,
        participant_label_ids={"personal": frozenset({"other"})},
    )
    add_event(
        harness,
        "二人だけのKaggle会議",
        window().start.isoformat(),
        window().end.isoformat(),
        label="other",
        organizer_email="b@example.com",
        creator_email="a@example.com",
        guest_emails=frozenset({"c@example.com"}),
        attendees_complete=True,
    )
    return harness, directory, service


def test_shared_meeting_blocks_only_organizer_and_guests_not_uninvited_creator(shared_meeting):
    _, _, service = shared_meeting
    assert find(service, slack_user_ids=["U1"])["free"]
    assert find(service, slack_user_ids=["U2"])["free"] == []
    assert find(service, slack_user_ids=["U3"])["free"] == []
    assert find(service)["free"] == []


def test_shared_calendar_organizer_resolves_inviter_and_guests(shared_meeting):
    harness, _, service = shared_meeting
    event = harness.calendar.stored["二人だけのKaggle会議"]
    harness.calendar.stored[event.id] = event.model_copy(
        update={
            "organizer_email": harness.calendar.calendars()[0].id,
            "creator_email": "b@example.com",
        }
    )
    assert find(service, slack_user_ids=["U1"])["free"]
    assert find(service, slack_user_ids=["U2"])["free"] == []
    assert find(service, slack_user_ids=["U3"])["free"] == []


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"attendees_complete": False}, "calendar_participants_incomplete"),
        ({"organizer_email": None}, "calendar_participants_incomplete"),
        ({"guest_emails": frozenset({"unknown@example.com"})}, "participant_not_found"),
    ],
)
def test_incomplete_shared_participants_never_silently_count_as_free(shared_meeting, changes, code):
    harness, _, service = shared_meeting
    event = harness.calendar.stored["二人だけのKaggle会議"]
    harness.calendar.stored[event.id] = event.model_copy(update=changes)
    with pytest.raises(AgentError) as caught:
        find(service)
    assert caught.value.code == code
