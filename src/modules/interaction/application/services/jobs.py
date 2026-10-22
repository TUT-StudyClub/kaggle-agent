from modules.interaction.application.dto.jobs import JobStatusOutput
from modules.interaction.domain.models import Source
from modules.interaction.domain.repositories import JobRepository


class JobManagement:
    def __init__(self, jobs: JobRepository):
        self._jobs = jobs

    def status(self, job_id: str) -> JobStatusOutput | None:
        job = self._jobs.get(job_id)
        return (
            JobStatusOutput(id=job.id, status=job.status, error_code=job.error_code)
            if job
            else None
        )

    def tracks(self, source: Source) -> bool:
        return self._jobs.tracks(source)

    def recover(self) -> int:
        return self._jobs.recover()

    def purge(self, before: float) -> int:
        return self._jobs.purge(before)
