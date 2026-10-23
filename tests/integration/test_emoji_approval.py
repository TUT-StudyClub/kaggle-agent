import time
from unittest.mock import Mock

import pytest
from slack_sdk.errors import SlackClientError

from apps.slack.handlers.events import register_listeners
from modules.interaction.domain.approval import is_approval_reaction, is_approval_reply
from modules.interaction.domain.models import ApprovalReaction, IncomingMessage, JobStatus
from modules.interaction.infrastructure.messaging.slack import SlackMessenger
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.planning.public import Plan
from modules.scheduling.public import CreateEvent
from shared.errors import AgentError
from tests.conftest import Harness
from tests.support import draft


def waiting(harness: Harness, *, delivered: bool = True):
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    job_id = harness.submit("予定を作って")
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job is not None and job.status == JobStatus.AWAITING_APPROVAL
    if delivered:
        notice_id, source, text = harness.jobs.pending_notifications()[0]
        client = Mock()
        client.chat_postMessage.return_value = {"ts": "2000.000001"}
        SlackMessenger(client, harness.policy, harness.jobs).deliver(notice_id, source, text)
    return job_id


def reaction(harness: Harness, name="+1", **overrides):
    return ApprovalReaction.model_validate(
        {
            "team_id": harness.source.team_id,
            "channel_id": harness.source.channel_id,
            "message_ts": "2000.000001",
            "user_id": harness.source.user_id,
            "reaction": name,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "name",
    [
        "+1",
        "thumbsup",
        "white_check_mark",
        "heavy_check_mark",
        "ballot_box_with_check",
        "ok",
        "ok_hand",
        "+1::skin-tone-3",
    ],
)
def test_positive_reaction_approves_exact_delivered_proposal_once(harness, name):
    job_id = waiting(harness)
    assert harness.agent.receive_reaction(reaction(harness, name))
    assert not harness.agent.receive_reaction(reaction(harness, name))
    harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert harness.calendar.writes == 1
    assert not harness.agent.run_once()


@pytest.mark.parametrize(
    "text",
    [
        "👍",
        "👍🏽",
        "✅",
        "☑️",
        "🆗",
        "👌",
        ":+1:",
        ":+1::skin-tone-3:",
        ":white_check_mark:",
        "OK",
        "ＯＫ！",
        "okです",
        "いいよ",
        "進めて",
        "実行してください",
        "承認します。",
        "👍 OK",
        "OK 👍",
        "👍 お願いします",
    ],
)
def test_short_reply_approves_only_pending_delivered_proposal_without_ai(harness, text):
    job_id = waiting(harness)
    assert (
        harness.agent.receive(IncomingMessage(event_id="answer", source=harness.source, text=text))
        is None
    )
    # 同じ返信を再受信しても新規ジョブを作らない。
    harness.agent.receive(IncomingMessage(event_id="answer", source=harness.source, text=text))
    harness.agent.run_once()
    assert harness.calendar.writes == 1
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert len(harness.planner.requests) == 1
    assert not harness.agent.run_once()


@pytest.mark.parametrize(
    "text",
    [
        "OK?",
        "OKかな？",
        "進めていい？",
        "まだ承認しない",
        "👍だけど待って",
        "OK、でも日時を変えて",
        "いいよとは言ってない",
        "👀",
        "🙏",
        "❤️",
        "😅",
        ":eyes:",
    ],
)
def test_ambiguous_negative_or_modified_reply_is_not_approval(text):
    assert not is_approval_reply(text)


@pytest.mark.parametrize(
    "name", ["eyes", "heart", "pray", "thinking_face", "-1", "custom_ok", "+1::skin-tone-9"]
)
def test_unapproved_reaction_does_not_approve(harness, name):
    job_id = waiting(harness)
    assert not is_approval_reaction(name)
    assert not harness.agent.receive_reaction(reaction(harness, name))
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL
    assert harness.calendar.writes == 0


@pytest.mark.parametrize(
    "override",
    [
        {"message_ts": "1000.000001"},
        {"message_ts": "9999.000001"},
        {"channel_id": "C2"},
        {"team_id": "TOTHER"},
        {"user_id": "U2"},
    ],
)
def test_wrong_message_channel_workspace_or_even_allowed_other_user_cannot_approve(
    harness, override
):
    harness.configure(
        policy=harness.policy.model_copy(update={"user_ids": frozenset({"U1", "U2"})})
    )
    job_id = waiting(harness)
    assert not harness.agent.receive_reaction(reaction(harness, **override))
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL


@pytest.mark.parametrize("source_changes", [{"thread_ts": "9999.000001"}, {"user_id": "U2"}])
def test_short_reply_requires_same_requester_and_thread(harness, source_changes):
    harness.configure(
        policy=harness.policy.model_copy(update={"user_ids": frozenset({"U1", "U2"})})
    )
    job_id = waiting(harness)
    result = harness.agent.receive(
        IncomingMessage(
            event_id="answer", source=harness.source.model_copy(update=source_changes), text="OK"
        )
    )
    assert result is None
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL
    assert not harness.agent.run_once()


@pytest.mark.parametrize("kind", ["reaction", "reply"])
def test_undelivered_or_expired_proposal_cannot_be_approved(harness, kind):
    job_id = waiting(harness, delivered=False)
    if kind == "reaction":
        assert not harness.agent.receive_reaction(reaction(harness))
    else:
        harness.agent.receive(IncomingMessage(event_id="answer", source=harness.source, text="OK"))
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL
    notice_id, source, text = harness.jobs.pending_notifications()[0]
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "2000.000001"}
    SlackMessenger(client, harness.policy, harness.jobs).deliver(notice_id, source, text)
    job = harness.jobs.get(job_id)
    harness.jobs.save(job.model_copy(update={"approval_expires_at": time.time() - 1}))
    if kind == "reaction":
        assert not harness.agent.receive_reaction(reaction(harness))
    else:
        harness.agent.receive(IncomingMessage(event_id="answer2", source=harness.source, text="OK"))
    assert not harness.agent.run_once()
    assert harness.calendar.writes == 0


def test_reaction_approval_and_delivery_survive_restart(harness):
    job_id = waiting(harness)
    harness.jobs = SQLiteJobRepository(harness.jobs.path)
    harness.configure()
    assert harness.agent.receive_reaction(reaction(harness))
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job is not None and job.status == JobStatus.SUCCEEDED


def test_approval_delivery_is_not_duplicated_and_unknown_result_is_not_reposted(harness):
    job_id = waiting(harness)
    notice_id, source, text = harness.jobs.pending_notifications()[0]
    client = Mock()
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    messenger.deliver(notice_id, source, text)
    client.chat_postMessage.assert_not_called()
    assert harness.jobs.get_delivery(notice_id).message_ts == "2000.000001"
    harness.jobs.forget_delivery(notice_id)
    client.chat_postMessage.side_effect = SlackClientError("response lost")
    with pytest.raises(AgentError):
        messenger.deliver(notice_id, source, text)
    with pytest.raises(AgentError):
        SlackMessenger(client, harness.policy, SQLiteJobRepository(harness.jobs.path)).deliver(
            notice_id, source, text
        )
    assert client.chat_postMessage.call_count == 1
    assert not harness.agent.receive_reaction(reaction(harness))
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL


def test_slack_reaction_handler_binds_bot_message_to_the_job(harness):
    job_id = waiting(harness)
    callbacks = {}
    app = Mock()
    app.event.side_effect = lambda name: lambda callback: callbacks.update({name: callback})
    register_listeners(app, harness.agent, "UBOT")
    event = {
        "type": "reaction_added",
        "reaction": "+1",
        "user": "U1",
        "item_user": "UBOT",
        "item": {"type": "message", "channel": "C1", "ts": "2000.000001"},
    }
    handler = callbacks["reaction_added"]
    handler({"team_id": "T1"}, event | {"item_user": "UOTHER"})
    handler({"team_id": "T1"}, event | {"item": {"type": "file"}})
    handler({"team_id": "T1"}, event | {"user": "UBOT"})
    handler({"team_id": "T1"}, event | {"item": {"type": "message", "ts": "invalid"}})
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL
    handler({"team_id": "T1"}, event)
    handler({"team_id": "T1"}, event)
    harness.agent.run_once()
    assert harness.calendar.writes == 1


def test_ambiguous_pending_targets_need_explicit_job_id(harness):
    first = waiting(harness)
    second = harness.submit("別の操作", "second")
    job = harness.jobs.get(second)
    harness.jobs.save(
        job.model_copy(
            update={
                "status": JobStatus.AWAITING_APPROVAL,
                "plan_json": harness.jobs.get(first).plan_json,
                "approval_expires_at": time.time() + 600,
            }
        )
    )
    assert not harness.jobs.approve_pending(harness.source, time.time())
    assert (
        harness.jobs.get(first).status
        == harness.jobs.get(second).status
        == JobStatus.AWAITING_APPROVAL
    )
