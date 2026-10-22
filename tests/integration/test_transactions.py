import json
import sqlite3

import pytest

from modules.interaction.domain.models import JobStatus
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository


def test_completion_commits_job_context_notification_audit_and_mapping_together(harness):
    job_id = harness.submit()
    running = harness.jobs.claim()
    assert running is not None
    result = json.dumps({"page": {"id": "page1"}})
    finished = running.transition(JobStatus.SUCCEEDED, result_json=result)
    harness.jobs.complete(
        finished, result, "完了", "succeeded", page_mapping=(harness.source.thread_key, "page1")
    )
    restarted = SQLiteJobRepository(harness.jobs.path)
    restored = restarted.get(job_id)
    assert restored is not None and restored.status == JobStatus.SUCCEEDED
    assert restarted.context(harness.source) == result
    assert restarted.mapping(harness.source.thread_key) == "page1"
    assert restarted.pending_notifications()[0][2] == "完了"
    with restarted.connection() as db:
        assert db.execute(
            "SELECT outcome FROM audit_logs WHERE job_id=?", (job_id,)
        ).fetchall() == [("succeeded",)]


def test_failed_completion_rolls_back_all_records(harness, monkeypatch):
    job_id = harness.submit()
    running = harness.jobs.claim()
    assert running is not None
    harness.jobs.remember(harness.source, '{"previous":true}')
    finished = running.transition(JobStatus.SUCCEEDED, result_json='{"new":true}')

    def fail_audit(*args):
        raise sqlite3.OperationalError("injected failure after notification insert")

    monkeypatch.setattr(harness.jobs, "_audit", fail_audit)
    with pytest.raises(sqlite3.OperationalError):
        harness.jobs.complete(
            finished,
            '{"new":true}',
            "完了",
            "succeeded",
            page_mapping=(harness.source.thread_key, "page1"),
        )
    restarted = SQLiteJobRepository(harness.jobs.path)
    restored = restarted.get(job_id)
    assert restored is not None and restored.status == JobStatus.RUNNING
    assert restarted.context(harness.source) == '{"previous":true}'
    assert restarted.mapping(harness.source.thread_key) is None
    assert restarted.pending_notifications() == []
    with restarted.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 0


def test_failed_approval_notification_rolls_back_state_and_outbox(harness, monkeypatch):
    job_id = harness.submit()
    running = harness.jobs.claim()
    assert running is not None
    waiting = running.transition(JobStatus.AWAITING_APPROVAL, plan_json="{}")

    def fail_audit(*args):
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(harness.jobs, "_audit", fail_audit)
    with pytest.raises(sqlite3.OperationalError):
        harness.jobs.notify(waiting, "承認してください", outcome="awaiting_approval")
    restarted = SQLiteJobRepository(harness.jobs.path)
    restored = restarted.get(job_id)
    assert restored is not None and restored.status == JobStatus.RUNNING
    assert restarted.pending_notifications() == []
