from modules.scheduling.application.dto.commands import (
    CreateEvent,
    UpdateEvent,
)
from modules.scheduling.domain.models import EventLabel
from modules.scheduling.domain.repositories import CalendarRepository
from shared.errors import AgentError


class CalendarAuthorization:
    def __init__(
        self,
        repository: CalendarRepository,
        writable_aliases: set[str],
        creation_labels: dict[str, EventLabel] | None = None,
    ):
        self.repository = repository
        self.writable_aliases = writable_aliases
        self.creation_labels = creation_labels or {}

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
        configured = self.creation_labels.get(action.calendar_alias)
        if action.draft.label is not None and action.draft.label != configured:
            raise AgentError(
                "forbidden_event_label", "新規予定には設定済みのイベントラベルだけを使ってね。"
            )
        if configured is not None and configured not in calendar.labels:
            # 色や名前の一致から別のラベルを選ばず、設定済みIDと属性をすべて検証する。
            raise AgentError(
                "creation_label_changed",
                "設定済みのイベントラベルが見つからないか、名前・色が変わっているよ。設定を確認してね。",
            )
        return action.model_copy(
            update={"draft": action.draft.model_copy(update={"label": configured})}
        )
