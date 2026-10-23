from modules.scheduling.application.dto.commands import (
    FindFree,
    ListCalendars,
    ListEvents,
)
from modules.scheduling.application.services.member_mapping import MemberMappingService
from modules.scheduling.application.use_cases.find_free import FindFreeUseCase
from modules.scheduling.domain.models import Event, TimeRange
from modules.scheduling.domain.repositories import CalendarRepository
from modules.scheduling.domain.services.member_mapping import resolve_label
from shared.errors import AgentError


class ReadScheduleUseCase:
    def __init__(self, repository: CalendarRepository, members: MemberMappingService):
        self.repository = repository
        self.members = members

    def events_for_label(self, alias: str, label_id: str, period: TimeRange) -> list[Event]:
        # チームラベルは個人のMember DBマッピングに依存しない。
        return [
            event for event in self.repository.events(alias, period) if event.label_id == label_id
        ]

    def execute(self, action: object, key: str) -> dict[str, object]:
        match action:
            case ListCalendars():
                calendars = []
                for calendar in self.repository.calendars():
                    members = self.members.load(calendar.alias)
                    mapping = self.members.mapping(calendar.alias, members)
                    known_labels = {label.id for label in calendar.labels}
                    if members is not None and not set(mapping) <= known_labels:
                        raise AgentError(
                            "member_label_missing",
                            "Member DBのラベルIDが対象カレンダーにありません。確認してください。",
                        )
                    calendars.append(
                        calendar.model_dump()
                        | {
                            "labels": [
                                label.model_dump() | {"slack_user_id": mapping.get(label.id)}
                                for label in calendar.labels
                            ],
                            "members": [m.model_dump(mode="json") for m in members or []],
                            "member_directory": members is not None,
                        }
                    )
                return {"calendars": calendars}
            case ListEvents():
                members = self.members.load(action.calendar_alias)
                label_id = action.label_id
                if action.slack_user_id:
                    label_id = resolve_label(
                        self.members.mapping(action.calendar_alias, members),
                        action.slack_user_id,
                        label_id,
                    )
                return {
                    "events": [
                        self.members.event_result(e, members)
                        for e in self.repository.events(action.calendar_alias, action.period)
                        if label_id is None or e.label_id == label_id
                    ]
                }
            case FindFree():
                return FindFreeUseCase(self.repository, self.members).execute(action)
            case _:
                raise AgentError("unsupported_action", "未対応の予定操作です。")
