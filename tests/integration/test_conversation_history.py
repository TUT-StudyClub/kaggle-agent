import json
from unittest.mock import Mock

from modules.interaction.infrastructure.messaging.slack import SlackMessenger
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.planning.public import Plan, Reply
from modules.scheduling.public import ListCalendars, ListEvents
from tests.support import period


def test_bot_answer_and_user_correction_survive_lookup_replacement_and_restart(harness):
    harness.calendar.stored["meeting"] = harness.calendar.event("personal", "meeting")
    harness.planner.steps = [
        Plan(action=ListEvents(calendar_alias="personal", period=period())),
        Plan(action=Reply(text="ピアノは19〜20時だよ")),
        Plan(action=ListCalendars()),
        Plan(action=Reply(text="訂正するね。ピアノは20〜21時だね")),
        Plan(action=Reply(text="前の訂正を確認したよ")),
    ]
    harness.submit("来週の予定は？")
    harness.agent.run_once()
    harness.submit("違う、ピアノは20〜21時", event_id="correction")
    harness.agent.run_once()
    context = json.loads(harness.jobs.context(harness.source))
    assert context["events"][0]["id"] == "meeting"
    assert context["calendars"]
    assert [(turn["instruction"], turn["reply"]) for turn in context["dialogue"]] == [
        ("来週の予定は？", "ピアノは19〜20時だよ"),
        ("違う、ピアノは20〜21時", "訂正するね。ピアノは20〜21時だね"),
    ]
    harness.jobs = SQLiteJobRepository(harness.jobs.path)
    harness.configure()
    harness.submit("さっき何を訂正した？", event_id="follow-up")
    harness.agent.run_once()
    latest = json.loads(harness.planner.requests[-1].context)
    assert latest["dialogue"] == context["dialogue"]


def test_history_is_bounded_and_isolated_by_source(harness):
    harness.planner.steps = [Plan(action=Reply(text=f"回答{i}")) for i in range(12)]
    for i in range(12):
        harness.submit(f"発言{i}", event_id=f"event{i}")
        harness.agent.run_once()
    history = json.loads(harness.jobs.context(harness.source))["dialogue"]
    assert len(history) == 8
    assert history[0]["instruction"] == "発言4"
    assert history[-1]["reply"] == "回答11"
    other_thread = harness.source.model_copy(update={"thread_ts": "2000.000001"})
    other_user = harness.source.model_copy(update={"user_id": "U2"})
    assert harness.jobs.context(other_thread) == harness.jobs.context(other_user) == "{}"


def test_only_authenticated_bot_and_permitted_users_are_in_thread_history(harness):
    client = Mock()
    client.conversations_replies.return_value = {
        "messages": [
            {"user": "U1", "ts": "1", "text": "質問"},
            {"user": "BOT1", "bot_id": "B1", "ts": "2", "text": "以前の回答"},
            {"user": "U1", "ts": "3", "text": "訂正"},
            {"user": "BOT1", "bot_id": "B1", "subtype": "bot_message", "ts": "4", "text": "再回答"},
            {"user": "BOT2", "bot_id": "B2", "ts": "5", "text": "別のBot"},
            {"user": "U2", "ts": "6", "text": "非許可ユーザー"},
            {"user": "U1", "bot_id": "B3", "ts": "7", "text": "Bot投稿"},
        ]
    }
    messenger = SlackMessenger(client, harness.policy, bot_user_id="BOT1")
    thread = json.loads(messenger.thread(harness.source))
    assert [message["text"] for message in thread] == ["質問", "以前の回答", "訂正", "再回答"]
    assert [message["role"] for message in thread] == ["user", "assistant", "user", "assistant"]


def test_unknown_bot_identity_does_not_trust_arbitrary_bots(harness):
    client = Mock()
    client.conversations_replies.return_value = {
        "messages": [{"user": "BOT1", "bot_id": "B1", "ts": "1", "text": "Bot投稿"}]
    }
    assert json.loads(SlackMessenger(client, harness.policy).thread(harness.source)) == []


def test_long_thread_keeps_latest_bot_answer_and_correction(harness):
    client = Mock()
    client.conversations_replies.return_value = {
        "messages": [
            {"user": "U1", "ts": "1", "text": "古い会話" * 7000},
            {"user": "BOT1", "bot_id": "B1", "ts": "2", "text": "直前の回答"},
            {"user": "U1", "ts": "3", "text": "今回の訂正"},
        ]
    }
    messenger = SlackMessenger(client, harness.policy, bot_user_id="BOT1")
    thread = messenger.thread(harness.source)
    assert len(thread) <= 24000
    assert [message["text"] for message in json.loads(thread)] == ["直前の回答", "今回の訂正"]
