import re
import unicodedata

APPROVAL_REACTIONS = frozenset(
    {
        "+1",
        "thumbsup",
        "white_check_mark",
        "heavy_check_mark",
        "ballot_box_with_check",
        "ok",
        "ok_hand",
    }
)


def is_approval_reaction(name: str) -> bool:
    base = re.sub(r"::skin-tone-[2-6]$", "", name)
    return base in APPROVAL_REACTIONS


def is_approval_reply(text: str) -> bool:
    value = unicodedata.normalize("NFKC", text).casefold().strip()
    value = value.rstrip("!。.").strip()
    # 肌色・絵文字の表示形式は意味を変えない。文中の👍だけでは承認にしない。
    value = re.sub("[\ufe0e\ufe0f\U0001f3fb-\U0001f3ff]", "", value)
    parts = value.split()
    if len(parts) > 1:
        return all(is_approval_reply(part) for part in parts)
    shortcode = re.fullmatch(r":([a-z0-9_+]+):(?::skin-tone-[2-6]:)?", value)
    if shortcode and is_approval_reaction(shortcode[1]):
        return True
    if value in {"👍", "✅", "☑", "✔", "🆗", "👌"}:
        return True
    return value in {
        "承認",
        "承認する",
        "承認します",
        "承認した",
        "承認しました",
        "ok",
        "okay",
        "okです",
        "オッケー",
        "オーケー",
        "いいよ",
        "いいです",
        "お願いします",
        "お願い",
        "進めて",
        "進めていいよ",
        "進めてください",
        "実行して",
        "実行してください",
        "それでいいよ",
        "それでお願いします",
    }
