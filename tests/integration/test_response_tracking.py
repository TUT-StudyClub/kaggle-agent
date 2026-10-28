import json
import time
from unittest.mock import Mock

import pytest

from apps.slack.handlers.events import register_listeners
from modules.interaction.domain.models import IncomingMessage, JobStatus
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.planning.public import Clarify, Plan, Reply
from shared.errors import AgentError
from tests.integration.test_emoji_approval import waiting


def listener(harness):
    callbacks = {}
    app = Mock()
    app.event.side_effect = lambda name: lambda callback: callbacks.update({name: callback})
    register_listeners(app, harness.agent, "BOT")

    def post(text, ts, *, user="U1", thread_ts="1000.000001", channel_type="channel"):
        callbacks["message"](
            {"team_id": "T1"},
            {
                "type": "message",
                "channel": "C1",
                "channel_type": channel_type,
                "user": user,
                "ts": ts,
                "thread_ts": thread_ts,
                "text": text,
            },
        )

    return post


def start(harness):
    harness.planner.steps = [Plan(action=Reply(text="19時からだよ"))]
    harness.submit("予定は？")
    harness.agent.run_once()
    harness.agent.deliver_notifications()
    harness.messenger.sent.clear()
    harness.messenger.activities.clear()
    harness.planner.requests.clear()


def test_followup_without_mention_is_answered(harness):
    start(harness)
    harness.planner.response_steps = [True]
    harness.planner.steps = [Plan(action=Reply(text="明日も大丈夫だよ"))]
    listener(harness)("それって明日でも大丈夫？", "1001.000001")
    assert harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert harness.messenger.sent == ["明日も大丈夫だよ"]
    assert not harness.planner.response_requests
    assert harness.planner.requests[0].instruction == "それって明日でも大丈夫？"


def test_approval_in_tracked_thread_uses_intent_not_response_decision(harness):
    job_id = waiting(harness)
    harness.planner.response_steps = [False]
    harness.planner.approval_steps = [True]
    listener(harness)("その内容で進めちゃって大丈夫です、お願いします！", "2001.000001")
    assert harness.agent.run_once()
    assert not harness.planner.response_requests
    assert harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert harness.calendar.writes == 1


@pytest.mark.parametrize(
    "text", ["ちょっと待って", "日程を変更してからお願いします", "承認したっけ？"]
)
def test_non_approval_does_not_execute_pending_operation(harness, text):
    job_id = waiting(harness)
    harness.planner.approval_steps = [False]
    harness.planner.response_steps = [False]
    listener(harness)(text, "2001.000001")
    assert harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL
    assert harness.calendar.writes == 0


@pytest.mark.parametrize("channel_type,prefix", [("im", ""), ("channel", "<@BOT> ")])
def test_natural_approval_works_in_dm_and_mention(harness, channel_type, prefix):
    job_id = waiting(harness)
    harness.planner.approval_steps = [True]
    listener(harness)(
        prefix + "その案で進めてもらえると助かります", "2001.000001", channel_type=channel_type
    )
    assert harness.agent.run_once()
    assert harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert harness.calendar.writes == 1


def test_approval_message_predating_proposal_cannot_approve_it(harness):
    job_id = waiting(harness)
    harness.planner.approval_steps = [True]
    listener(harness)("それでお願いします", "1999.000001")
    assert harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL
    assert harness.calendar.writes == 0


def test_approval_classifier_failure_does_not_write_and_explains_retry(harness):
    job_id = waiting(harness)
    harness.planner.approval_steps = [AgentError("ai_timeout", "timeout")]
    listener(harness)("その内容でお願いできますか", "2001.000001")
    assert harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL
    assert harness.calendar.writes == 0
    assert any("確認できなかった" in message for message in harness.messenger.sent)


def test_explicit_approval_with_multiple_pending_proposals_asks_for_reaction(harness):
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
    harness.planner.approval_steps = [True]
    listener(harness)("承認", "2001.000001")
    assert harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert any("特定できない" in text and "リアクション" in text for text in harness.messenger.sent)
    assert harness.jobs.get(first).status == JobStatus.AWAITING_APPROVAL
    assert harness.jobs.get(second).status == JobStatus.AWAITING_APPROVAL
    assert harness.calendar.writes == 0


def test_all_allowed_participants_are_answered_in_tracked_thread(harness):
    harness.configure(
        policy=harness.policy.model_copy(update={"user_ids": frozenset({"U1", "U2"})})
    )
    start(harness)
    harness.planner.steps = [Plan(action=Reply(text="応答したよ"))]
    listener(harness)("<@U1> どう思う？", "1001.000001", user="U2")
    assert harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert harness.messenger.sent == ["応答したよ"]
    assert harness.planner.requests[0].instruction == "<@U1> どう思う？"
    assert not harness.planner.response_requests


def test_untracked_thread_and_forbidden_user_are_ignored(harness):
    start(harness)
    post = listener(harness)
    post("質問", "1001.000001", thread_ts="9000.000001")
    post("質問", "1002.000001", user="FORBIDDEN")
    assert not harness.agent.run_once()
    assert not harness.planner.requests


def test_stop_applies_to_every_user_survives_restart_and_only_mention_resumes(harness):
    harness.configure(
        policy=harness.policy.model_copy(update={"user_ids": frozenset({"U1", "U2"})})
    )
    start(harness)
    other = harness.source.model_copy(update={"user_id": "U2"})
    harness.jobs.enqueue(IncomingMessage(event_id="other", source=other, text="質問"))
    assert harness.jobs.tracks(other)
    post = listener(harness)
    post("!stop", "1001.000001", user="U2")
    assert not harness.jobs.tracks(harness.source) and not harness.jobs.tracks(other)
    post("!stop", "1001.000001", user="U2")
    assert not harness.messenger.sent
    assert harness.messenger.reactions == [(other, "1001.000001", "thumbsup")]
    harness.jobs = SQLiteJobRepository(harness.jobs.path)
    harness.configure()
    post = listener(harness)
    post("続きの質問", "1002.000001")
    assert not harness.planner.response_requests
    assert harness.jobs.tracking_stopped(other)
    post("<@BOT> 続きを教えて", "1003.000001")
    assert not harness.jobs.tracking_stopped(other)
    assert harness.jobs.tracks(harness.source) and harness.jobs.tracks(other)
    # 再開前の古い停止イベント・メンションを再配信しても状態は戻らない。
    post("!stop", "1001.000001", user="U2")
    assert not harness.jobs.tracking_stopped(other)
    assert harness.messenger.reactions == [(other, "1001.000001", "thumbsup")]


def test_old_mention_does_not_resume_and_newer_stop_is_not_undone_by_duplicate(harness):
    post = listener(harness)
    post("<@BOT> 質問", "1001.000001")
    post("!stop", "1002.000001")
    post("<@BOT> 質問", "1001.000001")
    assert harness.jobs.tracking_stopped(harness.source)
    post("<@BOT> 再開", "1003.000001")
    post("!stop", "1004.000001")
    post("<@BOT> 再開", "1003.000001")
    assert harness.jobs.tracking_stopped(harness.source)


def test_stop_without_message_timestamp_does_not_react_to_thread_parent(harness):
    result = harness.agent.receive(
        IncomingMessage(event_id="stop", source=harness.source, text="!stop")
    )
    assert result is None
    assert harness.jobs.tracking_stopped(harness.source)
    assert not harness.messenger.sent and not harness.messenger.reactions


def test_reaction_failure_keeps_thread_stopped_without_reply(harness):
    harness.messenger.unavailable = True
    with pytest.raises(AgentError):
        listener(harness)("!stop", "1001.000001")
    assert harness.jobs.tracking_stopped(harness.source)
    assert not harness.messenger.sent


def test_stop_is_scoped_to_thread_and_forbidden_user_cannot_control_it(harness):
    start(harness)
    post = listener(harness)
    post("!stop", "1001.000001", user="FORBIDDEN")
    assert not harness.jobs.tracking_stopped(harness.source)
    assert not harness.messenger.reactions
    post("!stop", "1002.000001")
    post("<@BOT> 再開", "1003.000001", user="FORBIDDEN")
    assert harness.jobs.tracking_stopped(harness.source)
    other = harness.source.model_copy(update={"thread_ts": "9000.000001"})
    assert not harness.jobs.tracking_stopped(other)
    post("!stopについて教えて", "9001.000001", thread_ts=other.thread_ts)
    assert not harness.jobs.tracking_stopped(other)


def test_queued_followup_before_stop_is_ignored_even_after_resume(harness):
    start(harness)
    post = listener(harness)
    post("自動追跡の質問", "1001.000001")
    post("!stop", "1002.000001")
    post("<@BOT> 新しい質問", "1003.000001")
    assert harness.agent.run_once()
    assert not harness.planner.response_requests and not harness.planner.requests
    assert not harness.messenger.activities
    harness.planner.steps = [Plan(action=Reply(text="再開したよ"))]
    assert harness.agent.run_once()
    assert len(harness.planner.requests) == 1


def test_stop_during_ai_call_suppresses_followup_result(harness, monkeypatch):
    start(harness)
    post = listener(harness)
    harness.planner.response_steps = [True]
    harness.planner.steps = [Plan(action=Reply(text="送ってはいけない回答"))]
    name = "plan"
    original = getattr(harness.planner, name)

    def stop_then_return(request):
        post("!stop", "1002.000001")
        return original(request)

    monkeypatch.setattr(harness.planner, name, stop_then_return)
    post("続きの質問", "1001.000001")
    assert harness.agent.run_once()
    assert harness.jobs.pending_notifications() == []
    assert not harness.messenger.sent
    assert harness.messenger.reactions == [(harness.source, "1002.000001", "thumbsup")]


@pytest.mark.parametrize("respond", [True, False])
def test_short_approval_is_checked_before_it_can_authorize_a_write(harness, respond):
    job_id = waiting(harness)
    harness.messenger.activities.clear()
    harness.planner.response_steps = [respond]
    harness.planner.approval_steps = [respond]
    listener(harness)("OK", "3000.000001")
    # 応答先を確定するまでは承認されない。承認待ちでも判定ジョブは処理できる。
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL
    assert harness.agent.run_once()
    assert harness.calendar.writes == 0
    assert harness.jobs.get(job_id).status == (
        JobStatus.QUEUED if respond else JobStatus.AWAITING_APPROVAL
    )
    if respond:
        assert harness.agent.run_once()
        assert harness.calendar.writes == 1


def test_dm_and_explicit_mention_bypass_response_check(harness):
    post = listener(harness)
    harness.planner.steps = [Plan(action=Reply(text="DM回答")), Plan(action=Reply(text="明示回答"))]
    post("質問", "1001.000001", channel_type="im")
    harness.agent.run_once()
    post("<@BOT> 質問", "1002.000001")
    harness.agent.run_once()
    assert not harness.planner.response_requests
    assert len(harness.planner.requests) == 2


def test_planning_uses_only_history_up_to_current_message(harness):
    start(harness)
    harness.messenger.thread_text = json.dumps(
        [
            {"role": "assistant", "user": "BOT", "ts": "1000.500000", "text": "回答"},
            {"role": "user", "user": "U1", "ts": "1001.000001", "text": "質問"},
            {"role": "user", "user": "U2", "ts": "1002.000001", "text": "未来の発言"},
        ]
    )
    harness.planner.steps = [Plan(action=Reply(text="回答"))]
    listener(harness)("質問", "1001.000001")
    harness.agent.run_once()
    history = json.loads(harness.planner.requests[0].thread)
    assert [item["user"] for item in history] == ["BOT", "U1"]
    assert "未来の発言" not in harness.planner.requests[0].thread


def test_bare_mention_resumes_without_an_ai_request(harness):
    start(harness)
    post = listener(harness)
    post("!stop", "1001.000001")
    post("<@BOT>", "1002.000001")
    assert not harness.jobs.tracking_stopped(harness.source)
    assert harness.agent.tracks(harness.source)
    assert harness.messenger.sent[-1] == "このスレッドの追跡を再開したよ。"
    assert not harness.agent.run_once()
    assert not harness.planner.response_requests and not harness.planner.requests


def test_bare_mention_starts_tracking_before_first_job_and_survives_restart(harness):
    listener(harness)("<@BOT>", "1001.000001")
    assert harness.agent.tracks(harness.source)
    assert not harness.agent.run_once()
    assert not harness.messenger.sent
    harness.jobs = SQLiteJobRepository(harness.jobs.path)
    harness.configure()
    harness.planner.response_steps = [True]
    harness.planner.steps = [Plan(action=Reply(text="おはよう！"))]
    listener(harness)("おはよう", "1002.000001")
    assert harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert harness.messenger.sent == ["おはよう！"]
    assert harness.planner.requests[0].instruction == "おはよう"


def test_bare_mention_tracking_is_scoped_to_thread(harness):
    post = listener(harness)
    post("<@BOT>", "1001.000001")
    assert harness.agent.tracks(harness.source)
    assert harness.agent.tracks(harness.source.model_copy(update={"user_id": "U2"}))
    assert not harness.agent.tracks(harness.source.model_copy(update={"thread_ts": "9000.000001"}))
    post("!stop", "1002.000001")
    post("<@BOT>", "1001.000001")
    assert not harness.agent.tracks(harness.source)


def test_v5_migration_preserves_stop_and_existing_conversation(harness):
    start(harness)
    listener(harness)("!stop", "1001.000001")
    with harness.jobs.connection() as db:
        count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        db.execute("DROP TABLE conversation_tracking")
        db.execute("PRAGMA user_version=5")
    harness.jobs = SQLiteJobRepository(harness.jobs.path)
    harness.configure()
    assert harness.jobs.tracking_stopped(harness.source)
    assert not harness.agent.tracks(harness.source)
    listener(harness)("<@BOT>", "1002.000001")
    assert harness.agent.tracks(harness.source)
    with harness.jobs.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == count
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


def test_bot_clarification_keeps_original_request_for_short_answer(harness):
    harness.planner.steps = [Plan(action=Clarify(question="明日の19時でいい？"))]
    harness.submit("ミーティングの時間を相談したい")
    harness.agent.run_once()
    harness.planner.requests.clear()
    harness.planner.steps = [Plan(action=Reply(text="じゃあ明日19時だね"))]
    listener(harness)("OK", "1002.000001")
    assert harness.agent.run_once()
    pending = json.loads(harness.planner.requests[-1].context)["pending_clarification"]
    assert pending["requests"] == ["ミーティングの時間を相談したい"]
    assert pending["question"] == "明日の19時でいい？"
