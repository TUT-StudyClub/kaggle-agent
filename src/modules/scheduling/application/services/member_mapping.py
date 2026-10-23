from modules.scheduling.domain.models import CalendarMember, Event
from modules.scheduling.domain.repositories import MemberDirectory
from modules.scheduling.domain.services.member_mapping import ensure_unique_members
from shared.errors import AgentError

from ..dto.commands import CreateEvent


class MemberMappingService:
    def __init__(
        self,
        label_users: dict[str, dict[str, str]] | None = None,
        member_directory: MemberDirectory | None = None,
        participant_label_ids: dict[str, frozenset[str]] | None = None,
    ):
        self.label_users = label_users or {}
        self.member_directory = member_directory
        self.participant_label_ids = participant_label_ids or {}

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

    def prepare_invitation(self, action: CreateEvent, *, approved: bool = False) -> CreateEvent:
        selected = set(action.draft.guest_emails)
        if not action.guest_user_ids:
            if selected:
                raise AgentError(
                    "invitation_identity", "招待する人をMember DBから指定してください。"
                )
            return action.model_copy(update={"guest_names": []})
        members = self.load(action.calendar_alias)
        if not members:
            raise AgentError("invitation_directory", "招待する人のMember DBを設定してください。")
        by_user = {member.slack_user_id: member for member in members}
        addresses = []
        for user_id in action.guest_user_ids:
            member = by_user.get(user_id)
            if member is None or not member.calendar_emails:
                raise AgentError(
                    "invitation_missing_email",
                    f"<@{user_id}>の招待先が未登録だよ。"
                    "Member DBのCalendar Emailsを登録してくれる？",
                )
            candidates = member.calendar_emails
            chosen = candidates & selected
            if len(chosen) == 1:
                email = next(iter(chosen))
            elif len(candidates) == 1 and not chosen:
                email = next(iter(candidates))
            else:
                raise AgentError(
                    "invitation_ambiguous_email",
                    f"{member.name}の招待先はどれにする？ 候補: {', '.join(sorted(candidates))}",
                )
            if any(email in other.calendar_emails for other in members if other != member):
                raise AgentError(
                    "invitation_mapping_conflict",
                    "招待先メールがMember DBで複数人に対応しています。",
                )
            addresses.append(email)
        if selected - set(addresses):
            raise AgentError(
                "invitation_identity", "招待する人とメールアドレスの対応を確認してください。"
            )
        if approved and set(addresses) != selected:
            raise AgentError(
                "invitation_changed",
                "承認後に招待先が変わりました。最新の内容で再度依頼してください。",
            )
        draft = action.draft.model_copy(update={"guest_emails": tuple(addresses)})
        return action.model_copy(
            update={
                "draft": draft,
                "guest_names": [by_user[user].name for user in action.guest_user_ids],
            }
        )

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
