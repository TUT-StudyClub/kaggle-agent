from modules.scheduling.domain.models import CalendarMember, Event
from modules.scheduling.domain.repositories import MemberDirectory
from modules.scheduling.domain.services.member_mapping import ensure_unique_members


class MemberMappingService:
    def __init__(
        self,
        label_users: dict[str, dict[str, str]] | None = None,
        member_directory: MemberDirectory | None = None,
    ):
        self.label_users = label_users or {}
        self.member_directory = member_directory

    @property
    def uses_member_directory(self) -> bool:
        return self.member_directory is not None

    def load(self, alias: str) -> list[CalendarMember] | None:
        if self.member_directory is None or alias not in self.member_directory.calendar_aliases:
            return None
        members = self.member_directory.members(alias)
        ensure_unique_members(members)
        return members

    def mapping(self, alias: str, members: list[CalendarMember] | None) -> dict[str, str]:
        if members is not None:
            return {m.label_id: m.slack_user_id for m in members}
        return self.label_users.get(alias, {})

    def event_result(
        self, event: Event, members: list[CalendarMember] | None = None
    ) -> dict[str, object]:
        return event.model_dump() | {
            "slack_user_id": self.mapping(event.calendar_alias, members).get(event.label_id)
            if event.label_id
            else None,
            "member": next(
                (m.model_dump() for m in members or [] if m.label_id == event.label_id), None
            ),
        }
