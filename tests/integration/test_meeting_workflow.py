import json
import time

import pytest

from modules.interaction.application.ports.announcements import AnnouncementSender
from modules.interaction.application.services.meetings import MeetingService
from modules.interaction.domain.models import IncomingMessage, JobStatus
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.planning.public import CreateMeeting, MeetingAnnouncement, Plan
from modules.scheduling.composition import compose_scheduling
from modules.scheduling.domain.models import CalendarMember, EventLabel
from modules.scheduling.domain.repositories import MemberDirectory
from modules.scheduling.public import CreateEvent
from shared.errors import AgentError, UncertainWrite
from tests.conftest import Harness
from tests.support import draft


class Members(MemberDirectory):
    def __init__(self):
        self.rows = [
            CalendarMember(
                name="中川", slack_user_id="U2", label_id="l2", calendar_emails={"n@example.com"}
            ),
            CalendarMember(
                name="秋山", slack_user_id="U3", label_id="l3", calendar_emails={"a@example.com"}
            ),
        ]

    @property
    def calendar_aliases(self):
        return frozenset({"personal"})

    def members(self, calendar_alias):
        return self.rows


class Announcements(AnnouncementSender):
    def __init__(self, calendar):
        self.calendar = calendar
        self.sent = []
        self.fail = False
        self.uncertain = False
        self.available = True

    def channels(self):
        return [{"id": "C1", "name": "general"}] if self.available else []

    def send(self, channel_id, text, key):
        assert self.calendar.writes == 1  # 予定を検証してから周知する。
        if self.fail:
            raise AgentError("announcement_rejected", "拒否")
        self.sent.append((channel_id, text, key))
        if self.uncertain:
            raise UncertainWrite()
        return "2000.000001"

    def find(self, channel_id, text, key):
        return "2000.000001" if (channel_id, text, key) in self.sent else None


def action(**announcement_overrides):
    return CreateMeeting(
        event=CreateEvent(calendar_alias="personal", draft=draft(), guest_user_ids=["U2", "U3"]),
        announcement=MeetingAnnouncement(
            channel_id="C1", text="10/23 18:30〜19:00 面談を行うよ。", **announcement_overrides
        ),
    )


@pytest.fixture
def meeting(harness: Harness, monkeypatch):
    members = Members()
    announcements = Announcements(harness.calendar)
    calendars = harness.calendar.calendars()
    calendars[0] = calendars[0].model_copy(
        update={
            "labels": [EventLabel(id="l2", name="中川"), EventLabel(id="l3", name="秋山")],
        }
    )
    monkeypatch.setattr(harness.calendar, "calendars", lambda: calendars)
    harness.configure(member_directory=members, announcements=announcements)
    # Member DBの事前読取りをPythonが挟んでも、同じ操作案を返せるようにする。
    harness.planner.steps = [Plan(action=action(mention_channel=True))] * 2
    return harness, members, announcements


def prepare(harness):
    job_id = harness.submit("予定を作り中川と秋山を招待してgeneralに@channelで周知")
    assert harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job.status == JobStatus.AWAITING_APPROVAL
    return job_id, Plan.model_validate_json(job.plan_json).action


def test_one_approval_covers_verified_invites_and_announcement(meeting):
    harness, _, announcements = meeting
    job_id, approved = prepare(harness)
    assert approved.event.draft.guest_emails == ("n@example.com", "a@example.com")
    assert harness.calendar.writes == 0 and not announcements.sent
    preview = harness.jobs.pending_notifications()[0][2]
    for value in (
        "中川",
        "秋山",
        "n@example.com",
        "a@example.com",
        "#general",
        "@channel: 使用する",
    ):
        assert value in preview
    assert "<!channel>" not in preview
    harness.approve(job_id)
    assert harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert harness.calendar.writes == 1
    assert announcements.sent == [("C1", "<!channel>\n10/23 18:30〜19:00 面談を行うよ。", job_id)]
    assert harness.jobs.meeting(job_id).phase == "complete"
    harness.approve(job_id)
    assert not harness.agent.run_once()
    assert harness.calendar.writes == len(announcements.sent) == 1


def test_invited_members_are_mentioned_in_announcement(meeting):
    harness, _, announcements = meeting
    harness.planner.steps = [Plan(action=action())] * 2
    job_id, _ = prepare(harness)
    preview = harness.jobs.pending_notifications()[0][2]
    assert "メンション: 中川, 秋山" in preview
    assert "<@U2>" not in preview
    harness.approve(job_id)
    assert harness.agent.run_once()
    assert announcements.sent == [("C1", "<@U2> <@U3>\n10/23 18:30〜19:00 面談を行うよ。", job_id)]


@pytest.mark.parametrize("emails", [frozenset(), frozenset({"n@example.com", "other@example.com"})])
def test_missing_or_ambiguous_mail_becomes_clarification_without_writes(meeting, emails):
    harness, members, announcements = meeting
    members.rows[0] = members.rows[0].model_copy(update={"calendar_emails": emails})
    harness.submit()
    harness.agent.run_once()
    context = json.loads(harness.jobs.context(harness.source))
    assert "pending_clarification" in context
    assert "？" in context["pending_clarification"]["question"]
    assert harness.calendar.writes == 0 and not announcements.sent


def test_explicit_choice_uses_only_one_registered_mail(meeting):
    harness, members, _ = meeting
    members.rows[0] = members.rows[0].model_copy(
        update={
            "calendar_emails": frozenset({"n@example.com", "other@example.com"}),
        }
    )
    event = action().event
    event = event.model_copy(
        update={"draft": event.draft.model_copy(update={"guest_emails": ("other@example.com",)})}
    )
    harness.planner.steps = [Plan(action=action().model_copy(update={"event": event}))] * 2
    _, approved = prepare(harness)
    assert approved.event.draft.guest_emails == ("other@example.com", "a@example.com")


def test_changed_mail_after_approval_stops_before_calendar_write(meeting):
    harness, members, announcements = meeting
    job_id, _ = prepare(harness)
    members.rows[0] = members.rows[0].model_copy(
        update={"calendar_emails": frozenset({"new@example.com"})}
    )
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.FAILED
    assert harness.calendar.writes == 0 and not announcements.sent


def test_changed_channel_access_stops_before_calendar_write(meeting):
    harness, _, announcements = meeting
    job_id, _ = prepare(harness)
    announcements.available = False
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.jobs.get(job_id).error_code == "forbidden_channel"
    assert harness.calendar.writes == 0 and not announcements.sent


def test_failed_calendar_never_announces(meeting, monkeypatch):
    harness, _, announcements = meeting
    job_id, _ = prepare(harness)

    def create(*args):
        raise UncertainWrite()

    monkeypatch.setattr(harness.calendar, "create", create)
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.jobs.get(job_id).error_code == "uncertain_write"
    assert not announcements.sent
    assert not harness.agent.reconcile(job_id)


def test_rejected_slack_reports_partial_result_and_is_not_recreated(meeting):
    harness, members, announcements = meeting
    announcements.fail = True
    job_id, approved = prepare(harness)
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.jobs.get(job_id).error_code == "meeting_announcement_incomplete"
    assert harness.jobs.meeting(job_id).event["title"] == "面談"
    assert not harness.agent.reconcile(job_id)
    assert harness.jobs.get(job_id).status == JobStatus.FAILED
    # 永続化した進捗を読み直しても、同じジョブの予定・周知を新規送信しない。
    service = MeetingService(
        compose_scheduling(harness.calendar, {"personal"}, member_directory=members),
        announcements,
        SQLiteJobRepository(harness.jobs.path),
    )
    with pytest.raises(UncertainWrite):
        service.execute(approved, job_id)
    assert harness.calendar.writes == 1 and not announcements.sent


def test_accepted_slack_with_lost_response_reconciles_without_any_writes(meeting):
    harness, _, announcements = meeting
    announcements.uncertain = True
    job_id, _ = prepare(harness)
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.FAILED
    assert harness.agent.reconcile(job_id)
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert harness.calendar.writes == len(announcements.sent) == 1


def test_expired_or_wrong_actor_approval_never_writes(meeting):
    harness, _, announcements = meeting
    job_id, _ = prepare(harness)
    harness.agent.receive(
        IncomingMessage(
            event_id="wrong-user",
            source=harness.source.model_copy(update={"user_id": "U2"}),
            text="承認 " + job_id,
        )
    )
    assert not harness.agent.run_once()
    job = harness.jobs.get(job_id)
    harness.jobs.save(job.model_copy(update={"approval_expires_at": time.time() - 1}))
    harness.approve(job_id)
    assert not harness.agent.run_once()
    assert harness.calendar.writes == 0 and not announcements.sent


def test_meeting_cannot_bypass_disabled_calendar_create(meeting):
    harness, _, announcements = meeting
    tools = harness.skillbook.policy.tools.copy()
    tools["scheduling"] = tools["scheduling"].model_copy(
        update={
            "actions": tools["scheduling"].actions - {"calendar.create"},
        }
    )
    harness.skillbook = harness.skillbook.model_copy(
        update={
            "policy": harness.skillbook.policy.model_copy(update={"tools": tools}),
        }
    )
    harness.configure()
    job_id = harness.submit()
    harness.agent.run_once()
    assert harness.jobs.get(job_id).error_code == "forbidden_tool"
    assert harness.calendar.writes == 0 and not announcements.sent


def test_new_rules_and_workflow_operations_reach_planner(meeting):
    harness, _, _ = meeting
    prompt = harness.skillbook.prompt()
    for token in (
        "meeting.create",
        "slack.channels",
        "Calendar Emails",
        "まとめて1回承認",
        "mention_channel",
        "期限切れだけで結果不明と扱いません",
        "creation_label",
    ):
        assert token in prompt


@pytest.mark.parametrize("with_announcement", [False, True])
def test_invitation_without_announcement_and_announcement_without_guests(
    meeting, with_announcement
):
    harness, _, announcements = meeting
    if with_announcement:
        event = CreateEvent(calendar_alias="personal", draft=draft())
        selected = action().model_copy(update={"event": event})
    else:
        selected = action().event
    harness.planner.steps = [Plan(action=selected)] * 2
    job_id, approved = prepare(harness)
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert harness.calendar.writes == 1
    if with_announcement:
        assert len(announcements.sent) == 1
        assert "<!channel>" not in announcements.sent[0][1]
        assert not approved.event.draft.guest_emails
    else:
        assert not announcements.sent
        assert harness.calendar.stored[job_id].guest_emails == {"n@example.com", "a@example.com"}
