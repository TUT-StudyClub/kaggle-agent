from modules.interaction.domain.models import JobStatus
from shared.domain.value_object import ValueObject


class JobStatusOutput(ValueObject):
    id: str
    status: JobStatus
    error_code: str | None
