from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from apps.slack.handlers.events import register_listeners
from config import Settings
from modules.interaction.infrastructure.messaging.slack import SlackMessenger
from modules.interaction.public import Activity, AgentPolicy, ApprovalReaction, Source
from modules.planning.public import Plan, Reply
from shared.errors import AgentError


def policy(**changes):
    return AgentPolicy.model_validate(
        {
            "team_id": "T1",
            "user_ids": ["U1", "U2"],
            "channel_ids": ["C1", "D1", "D2", "G1"],
            "dm_user_ids": ["U1"],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "channel,user,allowed",
    [
        ("D1", "U1", True),
        ("D2", "U2", False),
        ("C1", "U2", True),
        ("G1", "U2", True),
        ("D3", "U1", False),
        ("C1", "OTHER", False),
    ],
)
def test_dm_restriction_does_not_change_channel_permissions(channel, user, allowed):
    source = Source(team_id="T1", channel_id=channel, thread_ts="1000.000001", user_id=user)
    assert policy().allows(source) is allowed
    assert not policy().allows(source.model_copy(update={"team_id": "OTHER"}))


def test_optional_dm_policy_preserves_existing_configuration_and_can_deny_all():
    peer = Source(team_id="T1", channel_id="D2", thread_ts="1000.000001", user_id="U2")
    assert policy(dm_user_ids=None).allows(peer)
    assert not policy(dm_user_ids=[]).allows(peer)
    assert policy(dm_user_ids=[]).allows(peer.model_copy(update={"channel_id": "C1"}))


def test_dm_allowlist_cannot_bypass_global_allowlist():
    with pytest.raises(ValidationError):
        policy(dm_user_ids=["UNAUTHORIZED"])


@pytest.mark.parametrize("text", ["こんにちは", "<@BOT> 質問", "!stop", "OK"])
def test_other_users_dm_is_silently_ignored_before_job_or_ai(harness, text):
    harness.configure(policy=policy())
    callbacks = {}
    app = Mock()
    app.event.side_effect = lambda name: lambda callback: callbacks.update({name: callback})
    register_listeners(app, harness.agent, "BOT")
    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D2",
        "user": "U2",
        "ts": "1000.000001",
        "text": text,
    }
    callbacks["message"]({"team_id": "T1"}, event)
    # DM内のメンションが別イベントで届いても拒否する。
    if "<@BOT>" in text:
        callbacks["app_mention"]({"team_id": "T1"}, event | {"type": "app_mention"})
    assert not harness.agent.run_once()
    assert not harness.messenger.sent and not harness.messenger.activities
    assert not harness.planner.requests and not harness.planner.response_requests
    with harness.jobs.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM thread_tracking").fetchone()[0] == 0


def test_owner_dm_and_other_users_channel_mention_still_work(harness):
    harness.configure(policy=policy())
    callbacks = {}
    app = Mock()
    app.event.side_effect = lambda name: lambda callback: callbacks.update({name: callback})
    register_listeners(app, harness.agent, "BOT")
    harness.planner.steps = [
        Plan(action=Reply(text="本人へのDM回答")),
        Plan(action=Reply(text="チャンネル回答")),
    ]
    callbacks["message"](
        {"team_id": "T1"},
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "ts": "1000.000001",
            "text": "質問",
        },
    )
    assert harness.agent.run_once()
    callbacks["app_mention"](
        {"team_id": "T1"},
        {
            "type": "app_mention",
            "channel": "C1",
            "user": "U2",
            "ts": "1001.000001",
            "text": "<@BOT> 質問",
        },
    )
    assert harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert harness.messenger.sent == ["本人へのDM回答", "チャンネル回答"]


@pytest.mark.parametrize("method", ["send", "deliver", "status", "thread"])
def test_dm_policy_also_blocks_outgoing_messages_status_and_history(method):
    client = Mock()
    messenger = SlackMessenger(client, policy())
    peer = Source(team_id="T1", channel_id="D2", thread_ts="1000.000001", user_id="U2")
    with pytest.raises(AgentError) as caught:
        if method == "send":
            messenger.send(peer, "送らない回答")
        elif method == "deliver":
            messenger.deliver("job/succeeded", peer, "送らない回答")
        elif method == "status":
            messenger.status(peer, Activity.THINKING)
        else:
            messenger.thread(peer)
    assert caught.value.code == "forbidden_source"
    assert not client.mock_calls


def test_other_users_dm_reaction_cannot_approve_or_resume_a_job(harness, monkeypatch):
    harness.configure(policy=policy())
    approve = Mock(return_value=True)
    clarify = Mock(return_value=True)
    monkeypatch.setattr(harness.jobs, "approve_reaction", approve)
    monkeypatch.setattr(harness.jobs, "answer_clarification_reaction", clarify)
    assert not harness.agent.receive_reaction(
        ApprovalReaction(
            team_id="T1",
            channel_id="D2",
            message_ts="1000.000001",
            user_id="U2",
            reaction="+1",
        )
    )
    approve.assert_not_called()
    clarify.assert_not_called()


def test_dm_user_ids_load_from_json_and_flow_to_policy(tmp_path, monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    path = tmp_path / ".env"
    path.write_text(
        'TEAM_ID=T1\nUSER_IDS=\'["U1","U2"]\'\nCHANNEL_IDS=\'["C1","D1","D2"]\'\nDM_USER_IDS=\'["U1"]\'\n'
    )
    settings = Settings.load(path)
    assert settings.agent_policy().dm_user_ids == frozenset({"U1"})
    monkeypatch.setenv("DM_USER_IDS", "[]")
    assert Settings.load(path).dm_user_ids == frozenset()
