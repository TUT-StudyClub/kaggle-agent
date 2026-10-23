from modules.scheduling.domain.models import CalendarMember, Event
from modules.scheduling.domain.repositories import MemberDirectory
from modules.scheduling.domain.services.member_mapping import ensure_unique_members
from shared.errors import AgentError


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

    def participant_users(self, alias: str, events: list[Event]) -> dict[str, frozenset[str]]:
        members = self.load(alias)
        if not members:
            raise AgentError(
                "participant_directory_missing", "参加者の対応をMember DBに設定してください。"
            )
        emails = {}
        for member in members:
            for email in member.calendar_emails:
                if email in emails and emails[email] != member.slack_user_id:
                    raise AgentError(
                        "participant_mapping_conflict",
                        "Calendar Emailsが複数人に登録されています。",
                    )
                emails[email] = member.slack_user_id
        result = {}
        for event in events:
            if (
                event.calendar_alias != alias
                or not event.participants_complete
                or not event.creator_email
            ):
                raise AgentError(
                    "calendar_participants_incomplete",
                    "予定の作成者・招待ゲストの全件を確認できません。",
                )
            participants = {email.strip().lower() for email in event.guest_emails}
            participants.add(event.creator_email.strip().lower())
            if not participants <= emails.keys():
                raise AgentError(
                    "participant_not_found",
                    "招待者・ゲストのメールをMember DBのCalendar Emailsに登録してください。",
                )
            result[event.id] = frozenset(emails[email] for email in participants)
        return result

    def event_result(
        self, event: Event, members: list[CalendarMember] | None = None
    ) -> dict[str, object]:
        return event.model_dump(mode="json") | {
            "slack_user_id": self.mapping(event.calendar_alias, members).get(event.label_id)
            if event.label_id
            else None,
            "member": next(
                (m.model_dump(mode="json") for m in members or [] if m.label_id == event.label_id),
                None,
            ),
        }
