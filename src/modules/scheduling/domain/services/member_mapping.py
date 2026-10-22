from modules.scheduling.domain.models import CalendarMember
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
