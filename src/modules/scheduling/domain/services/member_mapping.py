from modules.scheduling.domain.models import Calendar, CalendarMember, Event
from shared.errors import AgentError


def ensure_unique_members(members: list[CalendarMember]) -> None:
    if len({m.label_id for m in members}) != len(members) or len(
        {m.slack_user_id for m in members}
    ) != len(members):
        raise AgentError(
            "member_mapping_conflict",
            "Member DBでSlackユーザーIDまたはラベルIDが重複しています。確認してください。",
        )


def resolve_label(mapping: dict[str, str], slack_user_id: str, label_id: str | None) -> str:
    resolved = next((label for label, user in mapping.items() if user == slack_user_id), None)
    if resolved is None:
        raise AgentError(
            "member_not_found", "対象者のSlackユーザーとラベルの対応を確認してください。"
        )
    if label_id is not None and label_id != resolved:
        raise AgentError(
            "member_mapping_changed",
            "メンバーのラベル対応が変わっています。最新の対応を確認してください。",
        )
    return resolved


def resolve_event_participants(
    calendar: Calendar, event: Event, members: list[CalendarMember]
) -> frozenset[str]:
    # 共有カレンダー自身がorganizerなら、招待操作をしたcreatorを使う。
    # 人間のorganizerが指定されている場合にcreatorで置き換えない。
    host = event.creator_email if event.organizer_email == calendar.id else event.organizer_email
    if not host or not event.attendees_complete:
        raise AgentError(
            "calendar_participants_incomplete", "予定の主催者・招待ゲストの全件を確認できません。"
        )
    emails: dict[str, str] = {}
    for member in members:
        if not member.calendar_emails:
            raise AgentError(
                "participant_directory_missing", "Member DBのCalendar Emailsを登録してください。"
            )
        for email in member.calendar_emails:
            if email in emails and emails[email] != member.slack_user_id:
                raise AgentError(
                    "participant_mapping_conflict", "Calendar Emailsが複数人に登録されています。"
                )
            emails[email] = member.slack_user_id
    participants = {email.strip().lower() for email in event.guest_emails}
    participants.add(host.strip().lower())
    if not participants <= emails.keys():
        raise AgentError(
            "participant_not_found",
            "主催者・ゲストのメールをMember DBのCalendar Emailsに登録してください。",
        )
    return frozenset(emails[email] for email in participants)
