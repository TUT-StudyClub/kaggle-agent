from modules.scheduling.application.dto.commands import (
    CreateEvent,
    UpdateEvent,
)
from modules.scheduling.domain.repositories import CalendarRepository
from shared.errors import AgentError


class CalendarAuthorization:
    def __init__(self, repository: CalendarRepository, writable_aliases: set[str]):
        self.repository = repository
        self.writable_aliases = writable_aliases

    def prepare(self, action: CreateEvent | UpdateEvent) -> CreateEvent | UpdateEvent:
        calendar = next(
            (c for c in self.repository.calendars() if c.alias == action.calendar_alias),
            None,
        )
        if (
            calendar is None
            or action.calendar_alias not in self.writable_aliases
            or not calendar.owned
            or calendar.role != "owner"
        ):
            raise AgentError(
                "forbidden_calendar", "所有する書き込み許可済みカレンダーを選択してください。"
            )
        if isinstance(action, UpdateEvent):
            current = self.repository.event(action.calendar_alias, action.event_id)
            if not current.editable:
                raise AgentError(
                    "forbidden_event", "他人が主催する予定や編集不可の予定は変更できません。"
                )
            return action.model_copy(update={"expected_etag": current.etag})
        return action
