import json
from unittest.mock import Mock

import pytest

from modules.interaction.domain.models import IncomingMessage, JobStatus
from modules.interaction.infrastructure.messaging.slack import SlackMessenger
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.planning.public import Clarify, Plan, Reply
from modules.scheduling.public import CreateEvent
from tests.conftest import Harness
from tests.integration.test_emoji_approval import reaction
from tests.support import draft


def question(harness: Harness, *, delivered=True):
    harness.planner.steps = [Plan(action=Clarify(question="この内容で操作案を出していい？"))]
    job_id = harness.submit("招待と周知も含めた操作案を出して")
    harness.agent.run_once()
    if delivered:
        notice_id, source, text = harness.jobs.pending_notifications()[0]
        client = Mock()
        client.chat_startStream.return_value = {"ts": "2000.000001"}
        SlackMessenger(client, harness.policy, harness.jobs).deliver(notice_id, source, text)
        harness.jobs.notification_sent(notice_id)
    return job_id


@pytest.mark.parametrize("answer_type", ["reaction", "reply"])
def test_affirmative_answer_resumes_question_and_still_requires_operation_approval(
    harness, answer_type
):
    first = question(harness)
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    if answer_type == "reaction":
        assert harness.agent.receive_reaction(reaction(harness))
        assert not harness.agent.receive_reaction(reaction(harness, "white_check_mark"))
    else:
        assert harness.agent.receive(
            IncomingMessage(event_id="answer", source=harness.source, text="OK")
        )
    assert harness.calendar.writes == 0
    assert harness.agent.run_once()
    request = harness.planner.requests[-1]
    pending = json.loads(request.context)["pending_clarification"]
    assert pending["requests"] == ["招待と周知も含めた操作案を出して"]
    assert pending["question"] == "この内容で操作案を出していい？"
    assert harness.calendar.writes == 0
    with harness.jobs.connection() as db:
        rows = db.execute("SELECT payload FROM jobs WHERE status='awaiting_approval'").fetchall()
    assert len(rows) == 1
    resumed = json.loads(rows[0][0])
    assert resumed["id"] != first
    assert not resumed["write_started"]
    harness.approve(resumed["id"])
    harness.agent.run_once()
    assert harness.calendar.writes == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": "U2"},
        {"channel_id": "C2"},
        {"message_ts": "1000.000001"},
        {"message_ts": "9999.000001"},
        {"reaction": "eyes"},
    ],
)
def test_question_reaction_is_bound_to_requester_and_exact_message(harness, changes):
    harness.configure(
        policy=harness.policy.model_copy(update={"user_ids": frozenset({"U1", "U2"})})
    )
    question(harness)
    assert not harness.agent.receive_reaction(reaction(harness, **changes))
    assert not harness.agent.run_once()
    assert harness.calendar.writes == 0


def test_old_question_is_not_resumed_after_another_question_is_asked(harness):
    question(harness)
    harness.planner.steps = [Plan(action=Clarify(question="別の確認だよ。どの日にする？"))]
    harness.submit("日時を変更したい", "next")
    harness.agent.run_once()
    assert not harness.agent.receive_reaction(reaction(harness))
    assert not harness.agent.run_once()


def test_answer_does_not_resume_when_delivery_is_incomplete(harness):
    question(harness, delivered=False)
    assert not harness.agent.receive_reaction(reaction(harness))
    assert (
        harness.agent.receive(IncomingMessage(event_id="answer", source=harness.source, text="OK"))
        is None
    )
    assert not harness.agent.run_once()


def test_old_question_is_not_resumed_while_another_request_is_queued(harness):
    question(harness)
    queued = harness.submit("別の依頼", "queued")
    assert not harness.agent.receive_reaction(reaction(harness))
    assert harness.jobs.get(queued).status == JobStatus.QUEUED


def test_question_reaction_survives_restart_and_regular_answer_clears_it(harness):
    question(harness)
    harness.jobs = SQLiteJobRepository(harness.jobs.path)
    harness.configure()
    harness.planner.steps = [Plan(action=Reply(text="操作案を作るため、元の条件を確認したよ。"))]
    assert harness.agent.receive_reaction(reaction(harness))
    harness.agent.run_once()
    assert "pending_clarification" not in json.loads(harness.jobs.context(harness.source))
    assert not harness.agent.receive_reaction(reaction(harness))
    assert harness.calendar.writes == 0


def test_general_answer_reaction_does_not_become_new_instruction(harness):
    question(harness)
    context = json.loads(harness.jobs.context(harness.source))
    context.pop("pending_clarification")
    harness.jobs.remember(harness.source, json.dumps(context))
    assert not harness.agent.receive_reaction(reaction(harness))
    assert not harness.agent.run_once()
