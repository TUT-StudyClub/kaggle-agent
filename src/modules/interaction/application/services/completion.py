import json

from modules.interaction.domain.models import (
    Job,
    JobStatus,
    PendingClarification,
)
from modules.interaction.domain.repositories import JobRepository


class JobCompletion:
    def __init__(self, jobs: JobRepository):
        self.jobs = jobs

    def finish(
        self,
        job: Job,
        result: dict[str, object],
        text: str,
        clarification_question: str | None = None,
    ) -> None:
        # 外部ページ本文は除き、対象ID等と未解決の確認質問だけを会話へ残す。
        compact = json.loads(json.dumps(result))
        compact.pop("pending_clarification", None)
        for page in [compact.get("page"), *compact.get("pages", [])]:
            if isinstance(page, dict):
                page.pop("text", None)
        if clarification_question is not None:
            context = json.loads(self.jobs.context(job.message.source))
            previous = context.get("pending_clarification")
            requests = PendingClarification.model_validate(previous).requests if previous else []
            compact["pending_clarification"] = PendingClarification(
                requests=[*requests[-7:], job.message.text],
                question=clarification_question,
            ).model_dump()
        result_json = json.dumps(compact, ensure_ascii=False)
        finished = job.transition(JobStatus.SUCCEEDED, result_json=result_json)
        self.jobs.complete(finished, result_json, text, "succeeded")
