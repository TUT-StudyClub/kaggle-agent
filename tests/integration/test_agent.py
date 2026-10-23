import json
import time

import pytest

from modules.interaction.domain.models import IncomingMessage, JobStatus
from modules.knowledge.public import AppendPage, CreatePage, ReadPage, SearchPages
from modules.planning.public import Clarify, Plan, Reply
from modules.scheduling.public import CreateEvent, ListEvents, UpdateEvent
from shared.errors import AgentError
from tests.conftest import Harness
from tests.support import draft


def test_receipt_is_silent_and_final_reply_is_delivered(harness: Harness):
    harness.planner.steps = [Plan(action=Reply(text="回答です"))]
    job_id = harness.submit()
    assert harness.jobs.get(job_id)
    harness.agent.deliver_notifications()
    assert harness.messenger.sent == []
    assert harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert harness.messenger.sent == ["回答です"]


def test_notion_search_then_grounded_reply(harness: Harness):
    harness.planner.steps = [
        Plan(action=SearchPages(query="テスト")),
        Plan(action=Reply(text="https://notion.so/page1")),
    ]
    job_id = harness.submit()
    assert harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job and job.status == JobStatus.SUCCEEDED
    assert harness.planner.requests[1].observations[0]["pages"]
    assert harness.knowledge.writes == 0
    assert "機微な原文" not in harness.jobs.context(harness.source)


@pytest.mark.parametrize(
    "action",
    [
        CreatePage(parent_alias="notes", title="記録", body="概要", record_thread=True),
        AppendPage(page_id="page1", title="追記", body="決定事項"),
        CreateEvent(calendar_alias="personal", draft=draft()),
        UpdateEvent(calendar_alias="personal", event_id="event1", draft=draft()),
    ],
)
def test_writes_wait_for_owner_approval_and_are_not_replanned(harness: Harness, action):
    harness.planner.steps = [Plan(action=action)]
    job_id = harness.submit()
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job and job.status == JobStatus.AWAITING_APPROVAL
    assert harness.knowledge.writes + harness.calendar.writes == 0
    harness.approve(job_id)
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job and job.status == JobStatus.SUCCEEDED
    assert harness.knowledge.writes + harness.calendar.writes == 1
    assert len(harness.planner.requests) == 1
    harness.approve(job_id)
    assert not harness.agent.run_once()
    assert harness.knowledge.writes + harness.calendar.writes == 1


def test_duplicate_delivery_survives_repository_restart(harness: Harness):
    harness.planner.steps = [Plan(action=Reply(text="完了"))]
    harness.submit()
    harness.agent.run_once()
    assert (
        harness.agent.receive(
            IncomingMessage(event_id="event1", source=harness.source, text="再送")
        )
        is None
    )
    assert not harness.agent.run_once()


def test_approval_preview_is_code_and_preserves_embedded_fences(harness: Harness):
    action = CreatePage(parent_alias="notes", title="記録", body="例: ```python\nprint(1)\n```")
    harness.planner.steps = [Plan(action=action)]
    harness.submit()
    harness.agent.run_once()
    harness.agent.deliver_notifications()
    text = harness.messenger.sent[-1]
    assert text.count("```") == 2
    preview = text.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(preview) == action.model_dump(mode="json")
    assert text.endswith("\n```")
    assert harness.knowledge.writes == 0


@pytest.mark.parametrize(
    "field,value", [("team_id", "T2"), ("channel_id", "C2"), ("user_id", "U2")]
)
def test_unauthorized_sources_do_not_read_or_reply(harness: Harness, field: str, value: str):
    message = IncomingMessage(
        event_id="bad", source=harness.source.model_copy(update={field: value}), text="秘密を検索"
    )
    assert harness.agent.receive(message) is None
    assert not harness.agent.run_once()
    harness.agent.deliver_notifications()
    assert not harness.messenger.sent
    assert harness.messenger.thread_reads == 0


@pytest.mark.parametrize(
    "field,value", [("user_id", "U2"), ("channel_id", "C2"), ("thread_ts", "1001.000001")]
)
def test_approval_bound_to_actor_and_conversation(harness: Harness, field: str, value: str):
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    job_id = harness.submit()
    harness.agent.run_once()
    assert not harness.jobs.approve(
        job_id, harness.source.model_copy(update={field: value}), time.time()
    )
    assert not harness.agent.run_once()
    assert harness.calendar.writes == 0


def test_approval_expires_and_unblocks_conversation(harness: Harness):
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    job_id = harness.submit()
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job
    harness.jobs.save(job.model_copy(update={"approval_expires_at": time.time() - 1}))
    assert not harness.jobs.approve(job_id, harness.source, time.time())
    harness.jobs.recover()
    job = harness.jobs.get(job_id)
    assert job and job.error_code == "approval_expired"
    assert not job.write_started and harness.calendar.writes == 0
    harness.agent.deliver_notifications()
    assert "操作は実行していない" in harness.messenger.sent[-1]
    assert "照合" not in harness.messenger.sent[-1]


def test_follow_up_receives_last_event_id(harness: Harness):
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    job_id = harness.submit()
    harness.agent.run_once()
    harness.approve(job_id)
    harness.agent.run_once()
    harness.planner.steps = [Plan(action=Clarify(question="30分後ろでよいですか？"))]
    harness.submit("さっきの予定を30分後ろに", "event2")
    harness.agent.run_once()
    assert json.loads(harness.planner.requests[-1].context)["event"]["id"] == job_id


def test_shared_calendar_cannot_be_modified(harness: Harness):
    harness.calendar.role = "writer"
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    job_id = harness.submit()
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job and job.error_code == "forbidden_calendar"
    assert harness.calendar.writes == 0


def test_other_organizer_cannot_be_modified(harness: Harness):
    harness.calendar.editable = False
    harness.planner.steps = [
        Plan(action=UpdateEvent(calendar_alias="personal", event_id="e", draft=draft()))
    ]
    job_id = harness.submit()
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job and job.error_code == "forbidden_event"


def test_event_changed_after_approval_is_rejected(harness: Harness):
    harness.planner.steps = [
        Plan(action=UpdateEvent(calendar_alias="personal", event_id="e", draft=draft()))
    ]
    job_id = harness.submit()
    harness.agent.run_once()
    harness.calendar.etag = "changed"
    harness.approve(job_id)
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job and job.error_code == "conflict"
    assert harness.calendar.writes == 0


def test_recording_same_thread_reuses_page(harness: Harness):
    harness.jobs.map_page(harness.source.thread_key, "page1")
    harness.planner.steps = [
        Plan(action=CreatePage(parent_alias="notes", title="記録", body="概要", record_thread=True))
    ]
    job_id = harness.submit()
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job and job.plan_json
    assert Plan.model_validate_json(job.plan_json).action.kind == "notion.append"
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.knowledge.receipts[job_id] == "page1"


def test_ai_limit_stops_job_without_write(harness: Harness):
    harness.planner.steps = [AgentError("ai_limit", "利用枠に達しました")]
    job_id = harness.submit()
    harness.agent.run_once()
    harness.agent.deliver_notifications()
    job = harness.jobs.get(job_id)
    assert job and job.error_code == "ai_limit"
    assert harness.calendar.writes + harness.knowledge.writes == 0
    assert any("ai_limit" in text for text in harness.messenger.sent)


def test_uncertain_write_reconciles_without_second_write(harness: Harness):
    harness.knowledge.uncertain = True
    harness.planner.steps = [
        Plan(action=CreatePage(parent_alias="notes", title="記録", body="概要", record_thread=True))
    ]
    job_id = harness.submit()
    harness.agent.run_once()
    harness.approve(job_id)
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job and job.error_code == "uncertain_write"
    assert harness.agent.reconcile(job_id)
    assert harness.knowledge.writes == 1
    job = harness.jobs.get(job_id)
    assert job and job.status == JobStatus.SUCCEEDED
    assert harness.jobs.mapping(harness.source.thread_key) == "page2"


def test_unresolved_record_cannot_create_second_page(harness: Harness):
    harness.knowledge.uncertain = True
    action = CreatePage(parent_alias="notes", title="記録", body="概要", record_thread=True)
    harness.planner.steps = [Plan(action=action), Plan(action=action)]
    job_id = harness.submit()
    harness.agent.run_once()
    harness.approve(job_id)
    harness.agent.run_once()
    second = harness.submit("もう一度記録", "event2")
    harness.agent.run_once()
    job = harness.jobs.get(second)
    assert job and job.error_code == "unresolved_record"
    assert harness.knowledge.writes == 1


def test_notifications_survive_slack_outage(harness: Harness, monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    harness.planner.steps = [Plan(action=Reply(text="完了"))]
    harness.submit()
    harness.agent.run_once()
    harness.messenger.unavailable = True
    harness.agent.deliver_notifications()
    assert harness.jobs.pending_notifications()
    harness.messenger.unavailable = False
    harness.agent.deliver_notifications()
    assert harness.jobs.pending_notifications()
    monkeypatch.setattr(time, "monotonic", lambda: 106.0)
    harness.agent.deliver_notifications()
    assert not harness.jobs.pending_notifications()


def test_read_body_not_saved_in_context(harness: Harness):
    harness.planner.steps = [
        Plan(action=ReadPage(page_id="page1")),
        Plan(action=Reply(text="概要")),
    ]
    harness.submit()
    harness.agent.run_once()
    assert "機微な原文" not in harness.jobs.context(harness.source)


def test_clarification_keeps_request_across_short_answers_and_clears_after_reply(harness: Harness):
    harness.planner.steps = [
        Plan(action=ReadPage(page_id="page1")),
        Plan(action=Clarify(question="どのミーティングですか？")),
        Plan(action=Clarify(question="どこで確認しますか？")),
        Plan(action=ListEvents(calendar_alias="personal", period=draft().period)),
        Plan(action=Reply(text="カレンダーを確認しました。")),
    ]
    harness.submit(text="次のミーティングいつ？")
    harness.agent.run_once()
    pending = json.loads(harness.jobs.context(harness.source))["pending_clarification"]
    assert pending["requests"] == ["次のミーティングいつ？"]
    assert pending["question"] == "どのミーティングですか？"
    assert "機微な原文" not in harness.jobs.context(harness.source)
    harness.submit(text="キックオフ", event_id="answer1")
    harness.agent.run_once()
    harness.submit(text="カレンダー", event_id="answer2")
    harness.agent.run_once()
    pending = json.loads(harness.planner.requests[-2].context)["pending_clarification"]
    assert pending["requests"] == ["次のミーティングいつ？", "キックオフ"]
    assert pending["question"] == "どこで確認しますか？"
    assert "pending_clarification" not in json.loads(harness.jobs.context(harness.source))
    assert harness.calendar.writes == harness.knowledge.writes == 0


@pytest.mark.parametrize("has_citation", [False, True])
@pytest.mark.parametrize("clarify", [False, True])
def test_reply_preserves_text_without_appending_observed_event_links(
    harness: Harness, has_citation: bool, clarify: bool
):
    for index in range(20):
        event = harness.calendar.event("personal", str(index)).model_copy(
            update={"url": f"https://calendar.google.com/event/test-{index}"}
        )
        harness.calendar.stored[event.id] = event
    text = "予定を確認しました。"
    if has_citation:
        text += " https://calendar.google.com/event/test-19"
    harness.planner.steps = [
        Plan(action=ListEvents(calendar_alias="personal", period=draft().period)),
        Plan(action=Clarify(question=text) if clarify else Reply(text=text)),
    ]
    harness.submit()
    harness.agent.run_once()
    harness.agent.deliver_notifications()
    reply = harness.messenger.sent[-1]
    assert reply == text
