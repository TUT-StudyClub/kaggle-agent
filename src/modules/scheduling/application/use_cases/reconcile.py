from modules.scheduling.application.dto.commands import CreateEvent, UpdateEvent
from modules.scheduling.application.services.member_mapping import MemberMappingService
from modules.scheduling.domain.repositories import CalendarRepository


class ReconcileScheduleUseCase:
    def __init__(self, repository: CalendarRepository, members: MemberMappingService):
        self.repository = repository
        self.members = members

    def execute(self, action: object, key: str) -> dict[str, object] | None:
        if not isinstance(action, (CreateEvent, UpdateEvent)):
            return None
        event = self.repository.find_applied(
            action.calendar_alias,
            action.event_id if isinstance(action, UpdateEvent) else None,
            key,
        )
        if event is not None and isinstance(action, CreateEvent):
            if (
                event.title != action.draft.title
                or event.busy_period() != action.draft.period
                or (action.draft.label is not None and event.label != action.draft.label)
                or (action.draft.create_google_meet and not event.meet_url)
                or (
                    action.draft.guest_emails
                    and (
                        not event.attendees_complete
                        or {email.lower() for email in event.guest_emails}
                        != set(action.draft.guest_emails)
                    )
                )
            ):
                return None
        return {"event": self.members.event_result(event)} if event else None
