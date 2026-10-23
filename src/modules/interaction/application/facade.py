from modules.interaction.application.dto.jobs import JobStatusOutput
from modules.interaction.application.services.jobs import JobManagement
from modules.interaction.application.use_cases.deliver_notifications import (
    DeliverNotificationsUseCase,
)
from modules.interaction.application.use_cases.process_job import ProcessJobUseCase
from modules.interaction.application.use_cases.receive_message import ReceiveMessageUseCase
from modules.interaction.application.use_cases.reconcile_job import ReconcileJobUseCase
from modules.interaction.application.use_cases.remind_schedule import RemindScheduleUseCase
from modules.interaction.domain.models import IncomingMessage, Source


class InteractionFacade:
    def __init__(
        self,
        receive: ReceiveMessageUseCase,
        process: ProcessJobUseCase,
        reconcile: ReconcileJobUseCase,
        notifications: DeliverNotificationsUseCase,
        management: JobManagement,
        reminders: RemindScheduleUseCase | None = None,
    ):
        self._receive = receive
        self._process = process
        self._reconcile = reconcile
        self._notifications = notifications
        self._management = management
        self._reminders = reminders

    def remind_schedule(self) -> None:
        if self._reminders is not None:
            self._reminders.execute()

    def receive(self, message: IncomingMessage) -> str | None:
        return self._receive.execute(message)

    def run_once(self) -> bool:
        return self._process.execute()

    def reconcile(self, job_id: str) -> bool:
        return self._reconcile.execute(job_id)

    def deliver_notifications(self) -> None:
        self._notifications.execute()

    def tracks(self, source: Source) -> bool:
        return self._management.tracks(source)

    def recover(self) -> int:
        return self._management.recover()

    def purge(self, before: float) -> int:
        return self._management.purge(before)

    def status(self, job_id: str) -> JobStatusOutput | None:
        return self._management.status(job_id)
