import logging

from modules.interaction.application.ports.messenger import Activity, Messenger
from modules.interaction.application.services.activity import ActivityReporter
from modules.interaction.domain.repositories import JobRepository
from shared.application.ports.clock import Clock
from shared.errors import AgentError


class DeliverNotificationsUseCase:
    def __init__(
        self, jobs: JobRepository, messenger: Messenger, activity: ActivityReporter, clock: Clock
    ):
        self.jobs = jobs
        self.messenger = messenger
        self.activity = activity
        self.clock = clock

        self._notification_retry_at: dict[str, float] = {}
        self._notification_errors: dict[str, str] = {}

    def execute(self) -> None:
        for notice_id, source, text in self.jobs.pending_notifications():
            if self._notification_retry_at.get(notice_id, 0) > self.clock.monotonic():
                continue
            try:
                self.activity.update(source, Activity.SENDING)
                self.messenger.deliver(notice_id, source, text)
            except AgentError as error:
                self._notification_retry_at[notice_id] = self.clock.monotonic() + 5
                if self._notification_errors.get(notice_id) != error.code:
                    logging.getLogger(__name__).critical(
                        "Slack notification delivery failed: %s (%s)", notice_id, error.code
                    )
                self._notification_errors[notice_id] = error.code
                continue
            finally:
                self.activity.update(source, Activity.IDLE)
            self._notification_retry_at.pop(notice_id, None)
            self._notification_errors.pop(notice_id, None)
            self.jobs.notification_sent(notice_id)
