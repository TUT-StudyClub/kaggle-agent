import time

import pytest

from modules.interaction.domain.models import IncomingMessage, JobStatus
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.planning.public import Plan, Reply
from modules.scheduling.public import CreateEvent
from shared.errors import AgentError
from tests.conftest import Harness
from tests.support import draft


def test_crashed_read_job_is_queued_on_restart(harness: Harness):
    job_id = harness.submit()
    assert harness.jobs.claim()
    restarted = SQLiteJobRepository(harness.jobs.path)
    assert restarted.recover() == 1
    job = restarted.claim()
    assert job and job.id == job_id


def test_crashed_write_job_is_never_automatically_replayed(harness: Harness):
    job_id = harness.submit()
    job = harness.jobs.claim()
    assert job
    harness.jobs.save(job.model_copy(update={"write_started": True}))
    restarted = SQLiteJobRepository(harness.jobs.path)
    assert restarted.recover() == 1
    assert restarted.claim() is None
    job = restarted.get(job_id)
    assert job and job.status == JobStatus.FAILED and job.error_code == "uncertain_write"
    assert restarted.pending_notifications()


def test_same_conversation_serialized_but_other_thread_can_run(harness: Harness):
    harness.submit()
    harness.submit(event_id="event2")
    assert harness.jobs.claim()
    assert harness.jobs.claim() is None
    other = IncomingMessage(
        event_id="event3",
        source=harness.source.model_copy(
            update={"thread_ts": "2000.000001"},
        ),
        text="別スレッド",
    )
    assert harness.agent.receive(other)
    assert harness.jobs.claim()


def test_event_tombstones_survive_body_purge(harness: Harness):
    job_id = harness.submit()
    harness.jobs.purge(time.time() + 1)
    assert harness.jobs.get(job_id) is None
    assert (
        harness.jobs.enqueue(
            IncomingMessage(event_id="event1", source=harness.source, text="再配信")
        )
        is None
    )


def test_terminal_job_cannot_run_again(harness: Harness):
    harness.planner.steps = [Plan(action=Reply(text="完了"))]
    job_id = harness.submit()
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job
    with pytest.raises(AgentError, match="遷移"):
        job.transition(JobStatus.RUNNING)


def test_approved_job_precedes_later_messages(harness: Harness):
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    job_id = harness.submit()
    harness.agent.run_once()
    harness.submit("次の依頼", "event2")
    harness.approve(job_id)
    claimed = harness.jobs.claim()
    assert claimed and claimed.id == job_id and claimed.plan_json
