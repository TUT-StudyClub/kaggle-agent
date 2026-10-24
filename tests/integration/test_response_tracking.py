import json
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


def test_followup_without_mention_is_answered_after_response_check(harness):
    start(harness)
    harness.planner.response_steps = [True]
    harness.planner.steps = [Plan(action=Reply(text="明日も大丈夫だよ"))]
    listener(harness)("それって明日でも大丈夫？", "1001.000001")
    assert harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert harness.messenger.sent == ["明日も大丈夫だよ"]
    assert harness.planner.response_requests[0].requester_user_id == "U1"
    assert harness.planner.requests[0].instruction == "それって明日でも大丈夫？"


def test_human_conversation_has_no_reply_activity_tools_or_context_update(harness):
    start(harness)
    previous = harness.jobs.context(harness.source)
    harness.planner.response_steps = [False]
    listener(harness)("<@U2> どう思う？", "1001.000001")
    assert harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert not harness.messenger.sent and not harness.messenger.activities
    assert not harness.planner.requests
    assert harness.jobs.context(harness.source) == previous
    assert harness.knowledge.writes == harness.calendar.writes == 0
    with harness.jobs.connection() as db:
        assert db.execute(
            "SELECT outcome FROM audit_logs ORDER BY rowid DESC LIMIT 1"
        ).fetchone() == ("not_addressed",)
        assert db.execute("SELECT status FROM jobs ORDER BY rowid DESC LIMIT 1").fetchone() == (
            "ignored",
        )


@pytest.mark.parametrize("failure", [AgentError("ai_timeout", "timeout"), ValueError("broken")])
def test_response_check_failure_stays_silent(harness, monkeypatch, failure):
    start(harness)

    def fail(request):
        raise failure

    monkeypatch.setattr(harness.planner, "check_response", fail)
    listener(harness)("そうなんだ", "1001.000001")
    assert harness.agent.run_once()
    assert not harness.messenger.sent and not harness.messenger.activities
    assert not harness.jobs.pending_notifications() and not harness.planner.requests


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


@pytest.mark.parametrize("stop_during", ["check", "plan"])
def test_stop_during_ai_call_suppresses_followup_result(harness, monkeypatch, stop_during):
    start(harness)
    post = listener(harness)
    harness.planner.response_steps = [True]
    harness.planner.steps = [Plan(action=Reply(text="送ってはいけない回答"))]
    name = "check_response" if stop_during == "check" else "plan"
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
    listener(harness)("OK", "3000.000001")
    # 応答先を確定するまでは承認されない。承認待ちでも判定ジョブは処理できる。
    assert harness.jobs.get(job_id).status == JobStatus.AWAITING_APPROVAL
    assert harness.agent.run_once()
    assert not harness.messenger.activities
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


def test_response_check_uses_speakers_and_only_history_up_to_current_message(harness):
    start(harness)
    harness.messenger.thread_text = json.dumps(
        [
            {"role": "assistant", "user": "BOT", "ts": "1000.500000", "text": "回答"},
            {"role": "user", "user": "U1", "ts": "1001.000001", "text": "質問"},
            {"role": "user", "user": "U2", "ts": "1002.000001", "text": "未来の発言"},
        ]
    )
    harness.planner.response_steps = [False]
    listener(harness)("質問", "1001.000001")
    harness.agent.run_once()
    history = json.loads(harness.planner.response_requests[0].thread)
    assert [item["user"] for item in history] == ["BOT", "U1"]
    assert "未来の発言" not in harness.planner.response_requests[0].thread


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
    assert harness.planner.response_requests[0].instruction == "おはよう"


def test_bare_mention_tracking_is_scoped_to_user_and_thread(harness):
    post = listener(harness)
    post("<@BOT>", "1001.000001")
    assert harness.agent.tracks(harness.source)
    assert not harness.agent.tracks(harness.source.model_copy(update={"user_id": "U2"}))
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


def test_bot_clarification_keeps_original_request_after_classified_short_answer(harness):
    harness.planner.steps = [Plan(action=Clarify(question="明日の19時でいい？"))]
    harness.submit("ミーティングの時間を相談したい")
    harness.agent.run_once()
    previous = harness.jobs.context(harness.source)
    harness.planner.requests.clear()
    harness.planner.response_steps = [False, True]
    post = listener(harness)
    post("人同士の雑談", "1001.000001")
    harness.agent.run_once()
    assert harness.jobs.context(harness.source) == previous
    harness.planner.steps = [Plan(action=Reply(text="じゃあ明日19時だね"))]
    post("OK", "1002.000001")
    harness.agent.run_once()
    pending = json.loads(harness.planner.requests[-1].context)["pending_clarification"]
    assert pending["requests"] == ["ミーティングの時間を相談したい"]
    assert pending["question"] == "明日の19時でいい？"
