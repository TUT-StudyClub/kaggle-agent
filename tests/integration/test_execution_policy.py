from datetime import datetime, timedelta

from modules.interaction.domain.models import JobStatus
from modules.planning.public import Plan
from modules.scheduling.public import CreateEvent
from shared.application.ports.clock import Clock
from tests.support import draft


class FixedClock(Clock):
    def __init__(self):
        self.current = datetime.fromisoformat("2026-10-21T12:00:00+09:00")

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return 100.0


def test_approval_has_no_expiry_and_planning_time_uses_injected_clock(harness):
    clock = FixedClock()
    harness.configure(clock=clock)
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    job_id = harness.submit()
    harness.agent.run_once()
    waiting = harness.jobs.get(job_id)
    assert waiting.approval_expires_at is None
    assert harness.planner.requests[0].now == clock.current.isoformat()
    clock.current += timedelta(days=30)
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert harness.calendar.writes == 1


def test_worker_rechecks_actor_permissions_before_approved_write(harness):
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    job_id = harness.submit()
    harness.agent.run_once()
    harness.approve(job_id)
    harness.configure(policy=harness.policy.model_copy(update={"user_ids": frozenset({"U2"})}))
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job.status == JobStatus.FAILED and job.error_code == "forbidden_source"
    assert harness.calendar.writes == 0
