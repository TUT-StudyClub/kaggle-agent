from datetime import timedelta
from zoneinfo import ZoneInfo

from modules.scheduling.application.dto.availability import AvailabilityMember, AvailabilityResult
from modules.scheduling.application.dto.commands import FindFree
from modules.scheduling.application.services.member_mapping import MemberMappingService
from modules.scheduling.domain.models import TimeRange, free_slots
from modules.scheduling.domain.repositories import CalendarRepository
from modules.scheduling.domain.services.member_mapping import resolve_event_participants
from shared.errors import AgentError

JST = ZoneInfo("Asia/Tokyo")


class FindFreeUseCase:
    def __init__(self, repository: CalendarRepository, members: MemberMappingService):
        self.repository = repository
        self.members = members

    def execute(self, action: FindFree) -> dict[str, object]:
        # 対応表は毎回再取得する。個人ラベルのBusyと全体のfreeBusyを混ぜない。
        directories = {alias: self.members.load(alias) for alias in action.calendar_aliases}
        member_scope = any(value is not None for value in directories.values())
        selected: dict[str, AvailabilityMember] = {}
        busy: list[TimeRange] = []
        if member_scope:
            if any(not value for value in directories.values()):
                raise AgentError(
                    "availability_members_missing",
                    "対象カレンダーすべてに空でないMember DBが必要です。対象者を確認してください。",
                )
            requested = set(action.slack_user_ids or [])
            known = {m.slack_user_id for values in directories.values() for m in values or []}
            if requested - known:
                raise AgentError("member_not_found", "対象者がMember DBにありません。")
            calendars = {calendar.alias: calendar for calendar in self.repository.calendars()}
            for alias, members in directories.items():
                members = members or []
                calendar = calendars.get(alias)
                if calendar is None or not {m.label_id for m in members} <= {
                    label.id for label in calendar.labels
                }:
                    raise AgentError(
                        "member_label_missing", "Member DBのラベルIDが対象カレンダーにありません。"
                    )
                targets = [m for m in members if not requested or m.slack_user_id in requested]
                if not targets:
                    continue
                labels = {m.label_id for m in targets}
                participant_labels = self.members.participant_label_ids.get(alias, frozenset())
                if not participant_labels <= {label.id for label in calendar.labels}:
                    raise AgentError(
                        "participant_label_missing",
                        "参加者で判定する予定のラベルIDを確認してください。",
                    )
                for member in targets:
                    selected[member.slack_user_id] = AvailabilityMember(
                        name=member.name, slack_user_id=member.slack_user_id
                    )
                for event in self.repository.events(alias, action.period):
                    if event.calendar_alias != alias or not event.blocks_time:
                        continue
                    if event.label_id in participant_labels:
                        participants = resolve_event_participants(calendar, event, members)
                        if not participants & {m.slack_user_id for m in targets}:
                            continue
                    elif event.label_id not in labels:
                        continue
                    try:
                        period = event.busy_period()
                    except ValueError:
                        raise AgentError(
                            "availability_event_time",
                            "対象者の予定の開始・終了日時を確認できません。",
                        ) from None
                    if period is not None:
                        busy.append(period)
        else:
            if action.slack_user_ids is not None:
                raise AgentError(
                    "availability_members_missing",
                    "個人の空き時間にはMember DBを設定してください。",
                )
            busy = self.repository.busy(action.calendar_aliases, action.period)

        start = action.period.start.astimezone(JST)
        end = action.period.end.astimezone(JST)
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        slots = []
        while day < end:
            left = max(start, day + timedelta(minutes=action.daily_start_minute))
            right = min(end, day + timedelta(minutes=action.daily_end_minute))
            if left < right:
                slots.extend(free_slots(TimeRange(start=left, end=right), busy, action.minutes))
            day += timedelta(days=1)
        availability = AvailabilityResult(
            period=TimeRange(start=start, end=end),
            minutes=action.minutes,
            scope="members" if member_scope else "calendars",
            members=list(selected.values()),
            free=slots,
            daily_start_minute=action.daily_start_minute,
            daily_end_minute=action.daily_end_minute,
        )
        return {
            "availability": availability.model_dump(mode="json"),
            "free": [slot.model_dump(mode="json") for slot in slots],
        }
