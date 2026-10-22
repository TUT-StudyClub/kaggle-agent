import json

from modules.interaction.application.services.tools import ToolRegistry
from modules.interaction.domain.models import (
    JobStatus,
)
from modules.interaction.domain.repositories import JobRepository
from modules.knowledge.public import (
    AppendPage,
    CreatePage,
)
from modules.planning.public import (
    Plan,
)
from shared.errors import AgentError


class ReconcileJobUseCase:
    def __init__(self, jobs: JobRepository, tools: ToolRegistry):
        self.jobs = jobs
        self.tools = tools

    def execute(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if (
            job is None
            or not job.plan_json
            or not job.write_started
            or job.status != JobStatus.FAILED
        ):
            raise AgentError(
                "invalid_reconciliation", "結果不明の書き込みジョブを指定してください。"
            )
        plan = Plan.model_validate_json(job.plan_json)
        result = self.tools.reconcile(plan.action, job.id)
        if result is None:
            return False
        mapping = None
        if isinstance(plan.action, (CreatePage, AppendPage)) and plan.action.record_thread:
            mapping = (job.message.source.thread_key, str(result["page"]["id"]))
        serialized = json.dumps(result, ensure_ascii=False)
        confirmed = job.reconciled(serialized)
        self.jobs.complete(
            confirmed,
            serialized,
            "外部への再書き込みなしで実行済みと照合しました。\n" + serialized,
            "reconciled",
            page_mapping=mapping,
        )
        return True
