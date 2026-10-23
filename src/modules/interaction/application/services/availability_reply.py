from datetime import datetime
from zoneinfo import ZoneInfo

from modules.scheduling.public import AvailabilityResult

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"


def date_time(value: datetime) -> str:
    local = value.astimezone(JST)
    return f"{local.year}/{local.month}/{local.day}（{WEEKDAYS[local.weekday()]}）{local:%H:%M}"


def availability_reply(result: dict[str, object]) -> str:
    availability = AvailabilityResult.model_validate(result["availability"])
    lines = []
    if availability.scope == "members":
        names = "・".join(member.name for member in availability.members)
        lines.append(f"対象：{names}")
    else:
        lines.append("対象：指定カレンダー全体")
    lines.append(
        f"検索範囲：{date_time(availability.period.start)}〜"
        f"{date_time(availability.period.end)}（日本時間）"
    )
    if not availability.free:
        lines.append(
            f"この範囲には、登録済み予定と重ならない{availability.minutes}分の空きはないよ。"
        )
    else:
        lines.append(f"登録済み予定と重ならない、{availability.minutes}分以上の空き時間だよ。")
        for slot in availability.free[:10]:
            # 終了の00:00は翌日の日付を示し、別日の時刻と取り違えない。
            end = slot.end.astimezone(JST)
            end_text = f"{end:%H:%M}" if slot.start.date() == end.date() else date_time(end)
            lines.append(f"- {date_time(slot.start)}〜{end_text}")
        if len(availability.free) > 10:
            lines.append(f"ほか{len(availability.free) - 10}件の空き時間があるよ。")
    return "\n".join(lines)
