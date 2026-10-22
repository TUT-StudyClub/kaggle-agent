import json
from uuid import UUID

from modules.knowledge.domain.models import Note, Page
from modules.knowledge.domain.repositories import KnowledgeRepository
from shared.errors import AgentError, UncertainWrite
from shared.infrastructure.http import JsonHttp


def notion_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise AgentError("invalid_id", "Notion IDの形式が不正です。") from None


def plain(block: dict) -> str:
    content = block.get(block.get("type", ""), {})
    return "".join(
        t.get("plain_text", t.get("text", {}).get("content", ""))
        for t in content.get("rich_text", [])
    )


def rich_text(text: str) -> list[dict]:
    return [
        {"type": "text", "text": {"content": text[i : i + 2000]}} for i in range(0, len(text), 2000)
    ]


class NotionRepository(KnowledgeRepository):
    def __init__(
        self,
        http: JsonHttp,
        allowed_pages: set[str],
        parents: dict[str, str],
        data_sources: set[str] | None = None,
    ):
        self.http = http
        self.parents = {alias: notion_id(value) for alias, value in parents.items()}
        self.allowed = {notion_id(value) for value in allowed_pages} | set(self.parents.values())
        self.data_sources = {notion_id(value) for value in (data_sources or set())}

    def _page(self, page_id: str) -> dict:
        return self.http.request("GET", f"pages/{notion_id(page_id)}")

    def _allowed(self, page_id: str) -> bool:
        current = notion_id(page_id)
        for _ in range(12):
            if current in self.allowed:
                return True
            parent = self._page(current).get("parent", {})
            if parent.get("type") == "data_source_id":
                return notion_id(parent["data_source_id"]) in self.data_sources
            if parent.get("type") != "page_id":
                return False
            current = notion_id(parent["page_id"])
        return False

    def _authorize(self, page_id: str) -> str:
        value = notion_id(page_id)
        if not self._allowed(value):
            raise AgentError("forbidden_page", "許可されたNotionページの範囲外です。")
        return value

    def _children(self, block_id: str) -> list[dict]:
        result = []
        cursor = None
        for _ in range(20):
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.http.request("GET", f"blocks/{notion_id(block_id)}/children", params=params)
            result.extend(data["results"])
            if not data.get("has_more"):
                return result
            cursor = data["next_cursor"]
        raise AgentError("too_large", "ページが大きすぎます。対象範囲を絞ってください。")

    def _model(self, raw: dict, text: str = "") -> Page:
        title = "".join(
            t.get("plain_text", t.get("text", {}).get("content", ""))
            for prop in raw.get("properties", {}).values()
            if prop.get("type") == "title"
            for t in prop.get("title", [])
        )
        if raw.get("object") == "data_source":
            title = "".join(t.get("plain_text", "") for t in raw.get("title", []))
        url = raw.get("url")
        if not url and raw.get("object") == "data_source":
            database_id = raw.get("parent", {}).get("database_id")
            if not database_id:
                raise AgentError("unsupported_source", "親データベースを識別できません。")
            database = self.http.request("GET", f"databases/{notion_id(database_id)}")
            url = database["url"]
        return Page(
            kind="data_source" if raw.get("object") == "data_source" else "page",
            id=notion_id(raw["id"]),
            title=title,
            url=url or "",
            text=text,
            revision=raw.get("last_edited_time", ""),
        )

    def search(self, query: str) -> list[Page]:
        result = []
        cursor = None
        for _ in range(10):
            payload = {"query": query, "page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            raw = self.http.request("POST", "search", read=True, json=payload)
            for page in raw["results"]:
                if page.get("object") == "data_source":
                    if notion_id(page["id"]) in self.data_sources:
                        result.append(self._model(page))
                elif self._allowed(page["id"]):
                    result.append(self._model(page))
            if not raw.get("has_more"):
                return result
            cursor = raw["next_cursor"]
        raise AgentError("too_large", "検索結果が多すぎます。検索語を絞ってください。")

    def read(self, page_id: str) -> Page:
        if notion_id(page_id) in self.data_sources:
            return self._read_data_source(notion_id(page_id))
        page_id = self._authorize(page_id)
        lines = []

        def visit(block_id: str, depth: int) -> None:
            if depth > 6:
                raise AgentError("too_deep", "入れ子が深いため対象ページを絞ってください。")
            for block in self._children(block_id):
                if block["type"] in ("child_page", "child_database", "synced_block"):
                    continue
                lines.append(f"[block_id={block['id']}] {plain(block)}")
                if sum(map(len, lines)) > 24000:
                    raise AgentError("too_large", "本文が長すぎます。対象を絞ってください。")
                if block.get("has_children"):
                    visit(block["id"], depth + 1)

        visit(page_id, 0)
        return self._model(self._page(page_id), "\n".join(lines))

    def _read_data_source(self, data_source_id: str) -> Page:
        raw = self.http.request("GET", f"data_sources/{data_source_id}")
        rows = []
        cursor = None
        for _ in range(10):
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            result = self.http.request(
                "POST",
                f"data_sources/{data_source_id}/query",
                read=True,
                json=payload,
            )
            rows.extend(
                {"id": row["id"], "url": row["url"], "properties": row["properties"]}
                for row in result["results"]
            )
            text = json.dumps(
                {"schema": raw.get("properties", {}), "rows": rows}, ensure_ascii=False
            )
            if len(text) > 24000:
                raise AgentError(
                    "too_large", "データソースが大きすぎます。ページ単位で指定してください。"
                )
            if not result.get("has_more"):
                return self._model(
                    raw, "スキーマ・行プロパティ一覧。本文は各ページを参照。\n" + text
                )
            cursor = result["next_cursor"]
        raise AgentError("too_large", "データソースの取得上限に達しました。")

    def _note_block(self, note: Note, key: str) -> dict:
        text = f"[agent:{key}]\n{note.title}\n{note.body}\n出典: {note.source_url}"
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(text)}}

    def find_marker(self, page_id: str, key: str) -> bool:
        page_id = self._authorize(page_id)
        return any(f"[agent:{key}]" in plain(b) for b in self._children(page_id))

    def block_matches(self, page_id: str, block_id: str, text: str) -> bool:
        return plain(self._block(page_id, block_id)) == text

    def find_created(self, parent_alias: str, key: str) -> Page | None:
        parent = self._parent(parent_alias)
        for child in self._children(parent):
            if child["type"] == "child_page" and self.find_marker(child["id"], key):
                return self._model(self._page(child["id"]))
        return None

    def _parent(self, alias: str) -> str:
        if alias not in self.parents:
            raise AgentError("unknown_parent", "登録先の親ページを指定してください。")
        return self.parents[alias]

    def create(self, parent_alias: str, note: Note, key: str) -> Page:
        parent = self._parent(parent_alias)
        existing = self.find_created(parent_alias, key)
        if existing:
            return existing
        raw = self.http.request(
            "POST",
            "pages",
            json={
                "parent": {"type": "page_id", "page_id": parent},
                "properties": {"title": {"type": "title", "title": rich_text(note.title)}},
                "children": [self._note_block(note, key)],
            },
        )
        if not self.find_marker(raw["id"], key):
            raise UncertainWrite()
        return self._model(raw)

    def append(self, page_id: str, note: Note, key: str) -> Page:
        page_id = self._authorize(page_id)
        if not self.find_marker(page_id, key):
            self.http.request(
                "PATCH",
                f"blocks/{page_id}/children",
                json={
                    "children": [self._note_block(note, key)],
                },
            )
        if not self.find_marker(page_id, key):
            raise UncertainWrite()
        return self._model(self._page(page_id))

    def _block(self, page_id: str, block_id: str) -> dict:
        page_id = self._authorize(page_id)
        block_id = notion_id(block_id)
        block = self.http.request("GET", f"blocks/{block_id}")
        current = block
        for _ in range(12):
            parent = current.get("parent", {})
            if parent.get("type") == "page_id":
                if notion_id(parent["page_id"]) == page_id:
                    return block
                break
            if parent.get("type") != "block_id":
                break
            current = self.http.request("GET", f"blocks/{notion_id(parent['block_id'])}")
        raise AgentError("forbidden_block", "指定ブロックは対象ページに属していません。")

    def update_block(self, page_id: str, block_id: str, before: str, after: str, key: str) -> Page:
        block = self._block(page_id, block_id)
        if block["type"] != "paragraph" or block.get("has_children"):
            raise AgentError("unsupported_block", "MVPの部分編集は子を持たない段落のみ対応します。")
        current = plain(block)
        if current != after:
            if current != before:
                raise AgentError(
                    "conflict", "段落が変更されています。変更前の内容を再確認してください。"
                )
            self.http.request(
                "PATCH",
                f"blocks/{notion_id(block_id)}",
                json={
                    "paragraph": {"rich_text": rich_text(after)},
                },
            )
        if plain(self._block(page_id, block_id)) != after:
            raise UncertainWrite()
        return self._model(self._page(page_id))
