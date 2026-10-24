from unittest.mock import Mock

import pytest
from slack_sdk.errors import SlackApiError, SlackClientError

from modules.interaction.application.ports.messenger import Activity
from modules.interaction.infrastructure.messaging.slack import SlackMessenger
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.knowledge.public import SearchPages
from modules.planning.public import Plan, Reply
from shared.errors import AgentError
from tests.conftest import Harness


def rejection(code: str) -> SlackApiError:
    return SlackApiError("redacted", {"ok": False, "error": code})


def test_activity_tracks_real_work_and_does_not_post_receipts(harness: Harness):
    harness.planner.steps = [
        Plan(action=SearchPages(query="Kaggle")),
        Plan(action=Reply(text="回答")),
    ]
    harness.submit()
    assert harness.messenger.activities == []
    harness.agent.run_once()
    assert harness.messenger.activities == [
        Activity.READING,
        Activity.THINKING,
        Activity.NOTION,
        Activity.THINKING,
        Activity.IDLE,
    ]
    harness.agent.deliver_notifications()
    assert harness.messenger.activities[-2:] == [Activity.SENDING, Activity.IDLE]
    assert len(harness.messenger.sent) == 1


def test_display_failure_does_not_fail_job_or_leave_activity(harness: Harness, monkeypatch):
    def unavailable(source, activity):
        raise AgentError("slack_status", "offline")

    monkeypatch.setattr(harness.messenger, "status", unavailable)
    harness.planner.steps = [Plan(action=Reply(text="回答"))]
    job_id = harness.submit()
    harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert harness.messenger.sent == ["回答"]
    job = harness.jobs.get(job_id)
    assert job and job.status == "succeeded"


def test_error_clears_activity(harness: Harness):
    harness.planner.steps = [AgentError("ai_timeout", "timeout")]
    harness.submit()
    harness.agent.run_once()
    assert harness.messenger.activities[-1] == Activity.IDLE


def test_status_uses_native_indicator_without_posting(harness: Harness):
    client = Mock()
    messenger = SlackMessenger(client, harness.policy)
    messenger.status(harness.source, Activity.THINKING)
    messenger.status(harness.source, Activity.IDLE)
    assert [
        call.kwargs["status"] for call in client.assistant_threads_setStatus.call_args_list
    ] == [
        "考えています…",
        "",
    ]
    client.chat_postMessage.assert_not_called()


def test_reaction_targets_original_message_without_posting(harness: Harness):
    client = Mock()
    SlackMessenger(client, harness.policy).react(harness.source, "2000.000001", "thumbsup")
    client.reactions_add.assert_called_once_with(
        channel=harness.source.channel_id, timestamp="2000.000001", name="thumbsup"
    )
    client.chat_postMessage.assert_not_called()


def test_existing_reaction_is_success(harness: Harness):
    client = Mock()
    client.reactions_add.side_effect = rejection("already_reacted")
    SlackMessenger(client, harness.policy).react(harness.source, "2000.000001", "thumbsup")
    client.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("failure", [rejection("missing_scope"), SlackClientError("offline")])
def test_reaction_failure_does_not_fall_back_to_message(harness: Harness, failure):
    client = Mock()
    client.reactions_add.side_effect = failure
    with pytest.raises(AgentError) as error:
        SlackMessenger(client, harness.policy).react(harness.source, "2000.000001", "thumbsup")
    assert error.value.code == "slack_reaction"
    client.chat_postMessage.assert_not_called()


def test_reaction_rejects_forbidden_source(harness: Harness):
    client = Mock()
    source = harness.source.model_copy(update={"user_id": "FORBIDDEN"})
    with pytest.raises(AgentError):
        SlackMessenger(client, harness.policy).react(source, "2000.000001", "thumbsup")
    client.reactions_add.assert_not_called()


def test_stream_preserves_markdown_and_is_delivered_once(harness: Harness):
    client = Mock()
    client.chat_startStream.return_value = {"ok": True, "ts": "2000.000001"}
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    text = "## 予定\n\n**キックオフ**\n\n" + "- 詳細\n" * 200 + "[参照](https://example.com)"
    messenger.deliver("job/succeeded", harness.source, text)
    sent = client.chat_startStream.call_args.kwargs["markdown_text"]
    sent += "".join(
        call.kwargs["markdown_text"] for call in client.chat_appendStream.call_args_list
    )
    assert sent == text
    assert client.chat_startStream.call_args.kwargs["recipient_team_id"] == harness.source.team_id
    assert client.chat_startStream.call_args.kwargs["recipient_user_id"] == harness.source.user_id
    assert client.chat_appendStream.called
    client.chat_stopStream.assert_called_once_with(
        channel=harness.source.channel_id, ts="2000.000001"
    )
    messenger.deliver("job/succeeded", harness.source, text)
    client.chat_startStream.assert_called_once()
    client.chat_postMessage.assert_not_called()
    delivery = harness.jobs.get_delivery("job/succeeded")
    assert delivery and delivery.state == "done"
    assert delivery.mode == "stream"


@pytest.mark.parametrize("failure_method", ["chat_appendStream", "chat_stopStream"])
def test_interrupted_stream_repairs_same_reply_after_restart(harness: Harness, failure_method):
    client = Mock()
    client.chat_startStream.return_value = {"ts": "2000.000001"}
    getattr(client, failure_method).side_effect = SlackClientError(
        "connection lost after acceptance"
    )
    text = "**検証済み**\n" + "あ" * 1100
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    with pytest.raises(AgentError, match="中断"):
        messenger.deliver("job/succeeded", harness.source, text)
    store = SQLiteJobRepository(harness.jobs.path)
    client.chat_stopStream.side_effect = rejection("not_in_streaming_state")
    recovered = SlackMessenger(client, harness.policy, store)
    recovered.deliver("job/succeeded", harness.source, text)
    client.chat_startStream.assert_called_once()
    client.chat_postMessage.assert_not_called()
    client.chat_update.assert_called_once_with(
        channel=harness.source.channel_id,
        ts="2000.000001",
        markdown_text=text,
    )
    delivery = store.get_delivery("job/succeeded")
    assert delivery and delivery.state == "done"


@pytest.mark.parametrize(
    "result", [SlackClientError("response lost"), {"ok": True}, {"ts": "invalid"}]
)
def test_unknown_start_result_never_creates_another_reply(harness: Harness, result):
    client = Mock()
    if isinstance(result, Exception):
        client.chat_startStream.side_effect = result
    else:
        client.chat_startStream.return_value = result
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    with pytest.raises(AgentError):
        messenger.deliver("job/succeeded", harness.source, "回答")
    recovered = SlackMessenger(client, harness.policy, SQLiteJobRepository(harness.jobs.path))
    with pytest.raises(AgentError) as caught:
        recovered.deliver("job/succeeded", harness.source, "回答")
    assert caught.value.code == "slack_delivery_uncertain"
    client.chat_startStream.assert_called_once()
    client.chat_postMessage.assert_not_called()


def test_unavailable_stream_falls_back_only_before_stream_created(harness: Harness):
    client = Mock()
    client.chat_startStream.side_effect = rejection("method_not_supported_for_channel_type")
    client.chat_postMessage.return_value = {"ts": "2000.000001"}
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    messenger.deliver("job/succeeded", harness.source, "**回答**")
    messenger.deliver("job/succeeded", harness.source, "**回答**")
    client.chat_postMessage.assert_called_once()
    delivery = harness.jobs.get_delivery("job/succeeded")
    assert delivery and delivery.state == "done"
    assert delivery.mode == "message"


@pytest.mark.parametrize(
    "notice,text",
    [
        ("job/awaiting_approval", "承認案"),
        ("job/failed", "エラー"),
        ("job/succeeded", "あ" * 12001),
    ],
)
def test_approval_error_and_long_body_are_sent_atomically(harness: Harness, notice: str, text: str):
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "2000.000001"}
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    messenger.deliver(notice, harness.source, text)
    client.chat_startStream.assert_not_called()
    assert (
        client.chat_postMessage.call_args.kwargs.get(
            "markdown_text", client.chat_postMessage.call_args.kwargs.get("text")
        )
        == text
    )


def test_changed_delivery_content_is_rejected(harness: Harness):
    client = Mock()
    client.chat_startStream.return_value = {"ts": "2000.000001"}
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    messenger.deliver("job/succeeded", harness.source, "元の回答")
    with pytest.raises(AgentError) as caught:
        messenger.deliver("job/succeeded", harness.source, "別の回答")
    assert caught.value.code == "delivery_mismatch"


@pytest.mark.parametrize("operation", ["status", "deliver"])
def test_unauthorized_source_cannot_update_status_or_stream(harness: Harness, operation):
    client = Mock()
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    source = harness.source.model_copy(update={"user_id": "OTHER"})
    with pytest.raises(AgentError):
        if operation == "status":
            messenger.status(source, Activity.THINKING)
        else:
            messenger.deliver("job/succeeded", source, "回答")
    assert client.mock_calls == []


def test_v1_database_migration_preserves_jobs_and_notifications(harness: Harness):
    harness.planner.steps = [Plan(action=Reply(text="保持する回答"))]
    job_id = harness.submit()
    harness.agent.run_once()
    with harness.jobs.connection() as db:
        db.execute("DROP TABLE reply_deliveries")
        db.execute("PRAGMA user_version=1")
    upgraded = SQLiteJobRepository(harness.jobs.path)
    job = upgraded.get(job_id)
    assert job and job.status == "succeeded"
    assert upgraded.pending_notifications()[0][2] == "保持する回答"
    with upgraded.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
