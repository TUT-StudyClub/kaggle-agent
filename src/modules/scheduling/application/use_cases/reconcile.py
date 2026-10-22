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
        return {"event": self.members.event_result(event)} if event else None
