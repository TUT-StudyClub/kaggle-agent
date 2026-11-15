def plain_text(parts: list[dict]) -> str:
    """Notionのリッチテキスト配列を、装飾を除いた文字列にする。"""
    return "".join(
        part.get("plain_text", part.get("text", {}).get("content", "")) for part in parts
    )


def is_removed(row: dict) -> bool:
    """アーカイブ・ゴミ箱の行。APIの版によって異なるフラグ名をまとめて判定する。"""
    return bool(row.get("archived") or row.get("in_trash") or row.get("is_archived"))
