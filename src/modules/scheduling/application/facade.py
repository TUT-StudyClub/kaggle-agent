from ..domain.models import Event, TimeRange
from .dto.commands import CreateEvent, FindFree, ListCalendars, ListEvents, UpdateEvent
from .policies.calendar_authorization import CalendarAuthorization
from .services.member_mapping import MemberMappingService
from .use_cases.read import ReadScheduleUseCase
from .use_cases.reconcile import ReconcileScheduleUseCase
from .use_cases.write import WriteScheduleUseCase


class SchedulingService:
    def __init__(
        self,
        read: ReadScheduleUseCase,
        write: WriteScheduleUseCase,
        reconcile: ReconcileScheduleUseCase,
        authorization: CalendarAuthorization,
        members: MemberMappingService,
    ):
        self._read = read
        self._write = write
        self._reconcile = reconcile
        self._authorization = authorization
        self._members = members

    @property
    def uses_member_directory(self) -> bool:
        return self._members.uses_member_directory

    def events_for_label(self, alias: str, label_id: str, period: TimeRange) -> list[Event]:
        return self._read.events_for_label(alias, label_id, period)

    def execute(self, action: object, key: str) -> dict[str, object]:
        if isinstance(action, (ListCalendars, ListEvents, FindFree)):
            return self._read.execute(action, key)
        return self._write.execute(action, key)

    def prepare(self, action: CreateEvent | UpdateEvent) -> CreateEvent | UpdateEvent:
        return self._authorization.prepare(action)

    def reconcile(self, action: object, key: str) -> dict[str, object] | None:
        return self._reconcile.execute(action, key)
