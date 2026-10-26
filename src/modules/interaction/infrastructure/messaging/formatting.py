"""Render lists as Slack's native rich text, shared by every outgoing message."""

import re
from html import unescape

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline
from markdown_it.tree import SyntaxTreeNode

_SLACK = re.compile(
    r"<(?:@[UW][A-Z0-9]+|#[CG][A-Z0-9]+(?:\|[^>\n]+)?|!(?:channel|here|everyone)"
    r"|(?:https?://|mailto:)[^>\n]+)>"
)


def _slack_token(state: StateInline, silent: bool) -> bool:
    # Recognize only literal control syntax. Escaped source data such as
    # &lt;@U1&gt; must remain text even after Markdown decodes its entities.
    match = _SLACK.match(state.src, state.pos)
    if match is None:
        return False
    if not silent:
        state.push("slack", "", 0).content = match[0][1:-1]
    state.pos = match.end()
    return True


def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": False}).enable("strikethrough")
    parser.inline.ruler.before("autolink", "slack", _slack_token)
    return parser


def _normalize_bullets(text: str, parser: MarkdownIt) -> str:
    # Existing reminders use •. Keep code samples intact while allowing the
    # same native list conversion as Markdown's -, + and * markers.
    protected = set()
    for token in parser.parse(text):
        if token.type in {"fence", "code_block"} and token.map:
            protected.update(range(*token.map))
    # A code span can contain newlines too. Match delimiters of the same
    # length, including spans with embedded shorter backtick runs.
    runs = list(re.finditer(r"(?<![\\`])`+(?!`)", text))
    spans = []
    index = 0
    while index < len(runs):
        opening = runs[index]
        closing = next((i for i in range(index + 1, len(runs)) if runs[i][0] == opening[0]), None)
        if closing is None:
            index += 1
            continue
        spans.append((opening.start(), runs[closing].end()))
        index = closing + 1
    lines = []
    position = 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        match = re.match(r"^(\s*(?:>\s*)*)(•)(?=\s)", line)
        if match is not None and index not in protected:
            marker = position + match.start(2)
            if not any(start <= marker < end for start, end in spans):
                line = line[: match.start(2)] + "-" + line[match.end(2) :]
        lines.append(line)
        position += len(line)
    return "".join(lines)


def _text(text: str, style: dict | None = None) -> dict:
    return {"type": "text", "text": text, **({"style": style} if style else {})}


def _control(content: str) -> dict:
    if content.startswith("@"):
        return {"type": "user", "user_id": content[1:]}
    if content.startswith("#"):
        return {"type": "channel", "channel_id": content[1:].split("|", 1)[0]}
    if content.startswith("!"):
        return {"type": "broadcast", "range": content[1:]}
    url, separator, label = content.partition("|")
    return {
        "type": "link",
        "url": unescape(url),
        **({"text": unescape(label)} if separator else {}),
    }


def _inline(node: SyntaxTreeNode, *, slack_markup: bool, style: dict | None = None) -> list[dict]:
    style = style or {}
    if node.type == "slack":
        return [_control(node.content)]
    if node.type == "text":
        return [_text(node.content, style)] if node.content else []
    if node.type in {"softbreak", "hardbreak"}:
        return [_text("\n")]
    if node.type == "code_inline":
        return [_text(node.content, {**style, "code": True})]
    if node.type == "link":
        label = _inline_children(node, slack_markup=slack_markup)
        return [
            {
                "type": "link",
                "url": node.attrGet("href"),
                "text": "".join(element.get("text", "") for element in label),
                **({"style": style} if style else {}),
            }
        ]
    if node.type == "image":
        return [{"type": "link", "url": node.attrGet("src"), "text": node.content}]
    if node.type in {"fence", "code_block"}:
        return [_text(node.content, {"code": True})]
    formatting = {"strong": "bold", "em": "italic", "s": "strike", "heading": "bold"}
    if node.type in formatting:
        kind = formatting[node.type]
        if slack_markup and node.type == "em" and node.markup == "*":
            kind = "bold"
        style = {**style, kind: True}
    return _inline_children(node, slack_markup=slack_markup, style=style)


def _inline_children(
    node: SyntaxTreeNode, *, slack_markup: bool, style: dict | None = None
) -> list[dict]:
    return [
        element
        for child in node.children
        for element in _inline(child, slack_markup=slack_markup, style=style)
    ]


def _section(elements: list[dict]) -> dict:
    return {"type": "rich_text_section", "elements": elements or [_text(" ")]}


def _list(node: SyntaxTreeNode, slack_markup: bool, indent: int) -> list[dict]:
    ordered = node.type == "ordered_list"
    start = int(node.attrGet("start") or 1) - 1
    result: list[dict] = []
    current: dict | None = None
    for index, item in enumerate(node.children):
        if current is None:
            current = {
                "type": "rich_text_list",
                "style": "ordered" if ordered else "bullet",
                "indent": indent,
                "elements": [],
                **({"offset": start + index} if ordered else {}),
            }
            result.append(current)
        section = _section([])
        current["elements"].append(section)
        elements: list[dict] = []
        continuation = False
        for child in item.children:
            if child.type in {"bullet_list", "ordered_list"}:
                result.extend(_list(child, slack_markup, indent + 1))
                current = None
                continuation = True
            elif child.type in {"fence", "code_block"}:
                result.append(
                    {"type": "rich_text_preformatted", "elements": [_text(child.content)]}
                )
                current = None
                continuation = True
            elif continuation:
                # A paragraph following a sub-list stays after that sub-list;
                # it is a continuation of this item, not an extra bullet.
                result.append(_section(_inline(child, slack_markup=slack_markup)))
            else:
                if elements:
                    elements.append(_text("\n"))
                elements.extend(_inline(child, slack_markup=slack_markup))
                section["elements"] = elements or [_text(" ")]
    return result


def _blocks(node: SyntaxTreeNode, slack_markup: bool) -> list[dict]:
    result = []
    for child in node.children:
        if child.type in {"bullet_list", "ordered_list"}:
            result.extend(_list(child, slack_markup, 0))
        elif child.type in {"fence", "code_block"}:
            result.append({"type": "rich_text_preformatted", "elements": [_text(child.content)]})
        elif child.type == "blockquote":
            # Slack quotes cannot contain lists; render their lists natively
            # alongside the quoted paragraphs instead of flattening them.
            for element in _blocks(child, slack_markup):
                if element["type"] == "rich_text_section":
                    element = {**element, "type": "rich_text_quote"}
                result.append(element)
        elif child.type == "hr":
            result.append(_section([_text("────────")]))
        else:
            result.append(_section(_inline(child, slack_markup=slack_markup)))
    return result


def message_payload(text: str, *, slack_markup: bool = False) -> dict:
    """Keep the complete original text for notifications, history and reconciliation."""
    parser = _parser()
    tree = SyntaxTreeNode(parser.parse(_normalize_bullets(text, parser)))
    if any(node.type in {"bullet_list", "ordered_list"} for node in tree.walk()):
        return {
            "text": text,
            "blocks": [{"type": "rich_text", "elements": _blocks(tree, slack_markup)}],
            "mrkdwn": True,
        }
    if slack_markup or len(text) > 12000:
        return {"text": text, "mrkdwn": True}
    return {"markdown_text": text}
