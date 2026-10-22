from datetime import UTC, datetime

import pytest

from modules.interaction.domain.models import IncomingMessage, Job, JobStatus, Source
from shared.errors import AgentError


def job(status: JobStatus, *, plan_json: str | None = None) -> Job:
    return Job(
        id="job",
        message=IncomingMessage(
            event_id="event",
            text="依頼",
            source=Source(team_id="T1", user_id="U1", channel_id="C1", thread_ts="1.0"),
            received_at=datetime(2026, 10, 21, tzinfo=UTC),
        ),
        updated_at=0,
        status=status,
        plan_json=plan_json,
    )


@pytest.mark.parametrize(
    "status", [JobStatus.QUEUED, JobStatus.AWAITING_APPROVAL, JobStatus.SUCCEEDED, JobStatus.FAILED]
)
def test_write_cannot_start_outside_running_job(status):
    with pytest.raises(AgentError, match="承認済み"):
        job(status, plan_json="{}").start_write()


def test_running_read_cannot_become_a_write_without_saved_plan():
    with pytest.raises(AgentError):
        job(JobStatus.RUNNING).start_write()


def test_starting_write_keeps_original_job_immutable():
    before = job(JobStatus.RUNNING, plan_json="{}")
    after = before.start_write()
    assert not before.write_started
    assert after.write_started and after.status == JobStatus.RUNNING
