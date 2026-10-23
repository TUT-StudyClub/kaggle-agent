import json

from modules.interaction.domain.models import (
    ConversationTurn,
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
        context = json.loads(self.jobs.context(job.message.source))
        # 外部ページ本文は除き、対象ID等と未解決の確認質問だけを会話へ残す。
        compact = context | json.loads(json.dumps(result))
        compact.pop("pending_clarification", None)
        for page in [compact.get("page"), *compact.get("pages", [])]:
            if isinstance(page, dict):
                page.pop("text", None)
        if clarification_question is not None:
            previous = context.get("pending_clarification")
            requests = PendingClarification.model_validate(previous).requests if previous else []
            compact["pending_clarification"] = PendingClarification(
                requests=[*requests[-7:], job.message.text],
                question=clarification_question,
            ).model_dump()
        # 自分の回答と利用者の訂正を対で残す。他のBotの発言を混ぜない。
        turns = [
            ConversationTurn.model_validate(turn)
            for turn in context.get("dialogue", [])
            if turn.get("job_id") != job.id
        ]
        turns.append(
            ConversationTurn(
                job_id=job.id,
                instruction=job.message.text,
                reply=text,
                received_at=job.message.received_at,
            )
        )
        turns = turns[-8:]
        while len(turns) > 1 and sum(len(t.instruction) + len(t.reply) for t in turns) > 48000:
            turns.pop(0)
        compact["dialogue"] = [turn.model_dump(mode="json") for turn in turns]
        result_json = json.dumps(compact, ensure_ascii=False)
        finished = job.transition(JobStatus.SUCCEEDED, result_json=result_json)
        self.jobs.complete(finished, result_json, text, "succeeded")
