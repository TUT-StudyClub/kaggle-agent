from modules.scheduling.application.dto.commands import (
    CreateEvent,
    UpdateEvent,
)
from modules.scheduling.application.policies.calendar_authorization import CalendarAuthorization
from modules.scheduling.application.services.member_mapping import MemberMappingService
from modules.scheduling.domain.repositories import CalendarRepository
from shared.errors import AgentError


class WriteScheduleUseCase:
    def __init__(
        self,
        repository: CalendarRepository,
        members: MemberMappingService,
        authorization: CalendarAuthorization,
    ):
        self.repository = repository
        self.members = members
        self.authorization = authorization

    def execute(self, action: object, key: str) -> dict[str, object]:
        match action:
            case CreateEvent():
                if self.authorization.prepare(action) != action:
                    raise AgentError(
                        "creation_label_changed", "承認後に予定作成時のラベル設定が変わったよ。"
                    )
                action = self.members.prepare_invitation(action, approved=True)
                event = self.repository.create(action.calendar_alias, action.draft, key)
            case UpdateEvent():
                current = self.repository.event(action.calendar_alias, action.event_id)
                self.authorization.prepare(action)
                if current.etag != action.expected_etag:
                    raise AgentError(
                        "conflict", "承認待ちの間に予定が変更されました。再度依頼してください。"
                    )
                event = self.repository.update(action.calendar_alias, current, action.draft, key)
            case _:
                raise AgentError("unsupported_action", "未対応の予定操作です。")
        return {"event": self.members.event_result(event)}
