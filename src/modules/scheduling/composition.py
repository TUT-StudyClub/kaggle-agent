from shared.infrastructure.http import JsonHttp

from .application.dto.configuration import CalendarConfig
from .application.facade import SchedulingService
from .application.policies.calendar_authorization import CalendarAuthorization
from .application.services.member_mapping import MemberMappingService
from .application.use_cases.read import ReadScheduleUseCase
from .application.use_cases.reconcile import ReconcileScheduleUseCase
from .application.use_cases.write import WriteScheduleUseCase
from .domain.models import EventLabel
from .domain.repositories import CalendarRepository, MemberDirectory
from .infrastructure.external_services.google_calendar import GoogleCalendarRepository
from .infrastructure.external_services.notion_members import NotionMemberDirectory


def compose_scheduling(
    repository: CalendarRepository,
    writable_aliases: set[str],
    label_users: dict[str, dict[str, str]] | None = None,
    member_directory: MemberDirectory | None = None,
    participant_label_ids: dict[str, frozenset[str]] | None = None,
    creation_labels: dict[str, EventLabel] | None = None,
) -> SchedulingService:
    members = MemberMappingService(label_users, member_directory, participant_label_ids)
    authorization = CalendarAuthorization(repository, writable_aliases, creation_labels)
    return SchedulingService(
        ReadScheduleUseCase(repository, members),
        WriteScheduleUseCase(repository, members, authorization),
        ReconcileScheduleUseCase(repository, members),
        authorization,
        members,
    )


def build_scheduling(
    calendar_http: JsonHttp, notion_http: JsonHttp, calendars: dict[str, CalendarConfig]
) -> SchedulingService:
    directories = {
        alias: c.member_data_source_id
        for alias, c in calendars.items()
        if c.member_data_source_id is not None
    }
    return compose_scheduling(
        GoogleCalendarRepository(calendar_http, {alias: c.id for alias, c in calendars.items()}),
        {alias for alias, c in calendars.items() if c.writable},
        {alias: c.label_users for alias, c in calendars.items()},
        NotionMemberDirectory(notion_http, directories) if directories else None,
        {alias: c.participant_label_ids for alias, c in calendars.items()},
        {alias: c.creation_label for alias, c in calendars.items() if c.creation_label is not None},
    )
