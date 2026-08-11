"""Render an agent's markdown reply for Atlassian, instead of dumping it flat.

Every reply the platform posts went out as ONE plain-text paragraph: Jira got a
single ADF text node, Confluence got ``<p>{text}</p>``. So `**Verdict**`,
backticks, bullets and headings appeared literally, and the whole report ran
together as one block with no line breaks. That applies to every graph's replies,
not just triage.

This converts the subset of markdown the agents actually emit. Anything it does
not recognise degrades to a paragraph of plain text rather than being dropped, so
a reply is never lost to a formatting edge case.

Confluence output is escaped, which the old ``<p>{text}</p>`` was not: a reply
containing ``<`` or ``&`` produced invalid storage format.
"""

from __future__ import annotations

import html
import re
from typing import Any

_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_FENCE = re.compile(r"^\s*```\s*([\w+-]*)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DIVIDER = re.compile(r"^\s*\|[\s|:-]+\|\s*$")

# Inline spans, longest-delimiter first so ** wins over *.
_INLINE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<link>\[[^\]]+\]\([^)\s]+\))"
    r"|(?P<bold>\*\*[^*]+\*\*|__[^_]+__)"
    r"|(?P<italic>(?<![\w*])\*[^*\n]+\*(?![\w*])|(?<![\w_])_[^_\n]+_(?![\w_]))"
)


def _adf_marks(marks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop mark combinations ADF rejects.

    The ``code`` mark excludes every other formatting mark, so the very common
    ``**`abc1234`**`` would otherwise emit ``marks: [strong, code]`` and Jira
    answers 400 INVALID_INPUT for the whole comment. Code wins, since it carries
    the meaning; a link may ride along with it.
    """
    if any(m.get("type") == "code" for m in marks):
        return [m for m in marks if m.get("type") in ("code", "link")]
    return marks


def _inline_nodes(text: str, marks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Split a line into ADF text nodes carrying marks.

    Recurses through emphasis so nested spans keep both marks: the agents write
    ``**`abc1234`**`` constantly, and treating the bold body as literal text left
    the backticks visible in the comment.
    """
    inherited = marks or []

    def node(value: str, own: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        combined = _adf_marks(inherited + (own or []))
        out: dict[str, Any] = {"type": "text", "text": value}
        if combined:
            out["marks"] = combined
        return out

    nodes: list[dict[str, Any]] = []
    cursor = 0
    for match in _INLINE.finditer(text):
        if match.start() > cursor:
            nodes.append(node(text[cursor : match.start()]))
        kind = match.lastgroup
        raw = match.group()
        if kind == "code":
            # Code spans are literal: nothing inside them is markup.
            nodes.append(node(raw[1:-1], [{"type": "code"}]))
        elif kind == "link":
            label, _, rest = raw[1:].partition("](")
            href = rest[:-1]
            nodes.extend(
                _inline_nodes(label, inherited + [{"type": "link", "attrs": {"href": href}}])
            )
        elif kind == "bold":
            nodes.extend(_inline_nodes(raw[2:-2], inherited + [{"type": "strong"}]))
        else:
            nodes.extend(_inline_nodes(raw[1:-1], inherited + [{"type": "em"}]))
        cursor = match.end()
    if cursor < len(text):
        nodes.append(node(text[cursor:]))
    return nodes or [node(text)]


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _blocks(markdown: str) -> list[tuple[str, Any]]:
    """Group lines into (kind, payload) blocks: the shared parse for both formats."""
    blocks: list[tuple[str, Any]] = []
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    paragraph: list[str] = []
    bullets: list[str] = []
    ordered: list[str] = []
    table: list[str] = []
    fence: list[str] | None = None
    language = ""

    def flush() -> None:
        nonlocal paragraph, bullets, ordered, table
        if paragraph:
            blocks.append(("paragraph", " ".join(paragraph)))
            paragraph = []
        if bullets:
            blocks.append(("bullets", bullets))
            bullets = []
        if ordered:
            blocks.append(("ordered", ordered))
            ordered = []
        if table:
            rows = [row for row in table if not _TABLE_DIVIDER.match(row)]
            header = (
                _table_cells(rows[0]) if len(table) > 1 and _TABLE_DIVIDER.match(table[1]) else None
            )
            body = [_table_cells(row) for row in (rows[1:] if header else rows)]
            if header or body:
                blocks.append(("table", (header, body)))
            table = []

    for line in lines:
        fenced = _FENCE.match(line)
        if fence is not None:
            if fenced:
                blocks.append(("code", ("\n".join(fence), language)))
                fence, language = None, ""
            else:
                fence.append(line)
            continue
        if fenced:
            flush()
            fence, language = [], fenced.group(1)
            continue
        if not line.strip():
            flush()
            continue
        if _TABLE_ROW.match(line):
            if paragraph or bullets or ordered:
                flush()
            table.append(line)
            continue
        if table:
            flush()
        if _RULE.match(line):
            flush()
            blocks.append(("rule", None))
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            blocks.append(("heading", (len(heading.group(1)), heading.group(2).strip())))
            continue
        bullet = _BULLET.match(line)
        if bullet:
            if paragraph or ordered:
                flush()
            bullets.append(bullet.group(1).strip())
            continue
        numbered = _ORDERED.match(line)
        if numbered:
            if paragraph or bullets:
                flush()
            ordered.append(numbered.group(1).strip())
            continue
        if bullets or ordered:
            # A continuation line under a list item belongs to that item.
            (bullets or ordered)[-1] += f" {line.strip()}"
            continue
        paragraph.append(line.strip())

    if fence is not None:
        blocks.append(("code", ("\n".join(fence), language)))
    flush()
    return blocks


def markdown_to_adf(markdown: str) -> dict[str, Any]:
    """An ADF document for a Jira comment."""
    content: list[dict[str, Any]] = []
    for kind, payload in _blocks(markdown):
        if kind == "paragraph":
            content.append({"type": "paragraph", "content": _inline_nodes(payload)})
        elif kind == "heading":
            level, text = payload
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": min(max(level, 1), 6)},
                    "content": _inline_nodes(text),
                }
            )
        elif kind in ("bullets", "ordered"):
            content.append(
                {
                    "type": "bulletList" if kind == "bullets" else "orderedList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [{"type": "paragraph", "content": _inline_nodes(item)}],
                        }
                        for item in payload
                    ],
                }
            )
        elif kind == "code":
            text, language = payload
            node: dict[str, Any] = {
                "type": "codeBlock",
                "content": [{"type": "text", "text": text}] if text else [],
            }
            if language:
                node["attrs"] = {"language": language}
            content.append(node)
        elif kind == "table":
            header, body = payload
            rows: list[dict[str, Any]] = []
            if header:
                rows.append(
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "attrs": {},
                                "content": [{"type": "paragraph", "content": _inline_nodes(cell)}],
                            }
                            for cell in header
                        ],
                    }
                )
            for row in body:
                rows.append(
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableCell",
                                "attrs": {},
                                "content": [{"type": "paragraph", "content": _inline_nodes(cell)}],
                            }
                            for cell in row
                        ],
                    }
                )
            content.append(
                {
                    "type": "table",
                    "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                    "content": rows,
                }
            )
        elif kind == "rule":
            content.append({"type": "rule"})
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": markdown or ""}]}]
    return {"type": "doc", "version": 1, "content": content}


def _storage_inline(text: str) -> str:
    out, cursor = [], 0
    for match in _INLINE.finditer(text):
        if match.start() > cursor:
            out.append(html.escape(text[cursor : match.start()]))
        kind, raw = match.lastgroup, match.group()
        if kind == "code":
            out.append(f"<code>{html.escape(raw[1:-1])}</code>")
        elif kind == "link":
            label, _, rest = raw[1:].partition("](")
            href = html.escape(rest[:-1], quote=True)
            out.append(f'<a href="{href}">{_storage_inline(label)}</a>')
        elif kind == "bold":
            out.append(f"<strong>{_storage_inline(raw[2:-2])}</strong>")
        else:
            out.append(f"<em>{_storage_inline(raw[1:-1])}</em>")
        cursor = match.end()
    if cursor < len(text):
        out.append(html.escape(text[cursor:]))
    return "".join(out) or html.escape(text)


def markdown_to_storage(markdown: str) -> str:
    """Confluence storage format for a footer comment, HTML-escaped."""
    parts: list[str] = []
    for kind, payload in _blocks(markdown):
        if kind == "paragraph":
            parts.append(f"<p>{_storage_inline(payload)}</p>")
        elif kind == "heading":
            level, text = payload
            tag = f"h{min(max(level, 1), 6)}"
            parts.append(f"<{tag}>{_storage_inline(text)}</{tag}>")
        elif kind in ("bullets", "ordered"):
            tag = "ul" if kind == "bullets" else "ol"
            items = "".join(f"<li>{_storage_inline(item)}</li>" for item in payload)
            parts.append(f"<{tag}>{items}</{tag}>")
        elif kind == "code":
            text, _language = payload
            parts.append(
                '<ac:structured-macro ac:name="code">'
                f"<ac:plain-text-body><![CDATA[{text}]]></ac:plain-text-body>"
                "</ac:structured-macro>"
            )
        elif kind == "table":
            header, body = payload
            rows = []
            if header:
                cells = "".join(f"<th>{_storage_inline(cell)}</th>" for cell in header)
                rows.append(f"<tr>{cells}</tr>")
            for row in body:
                cells = "".join(f"<td>{_storage_inline(cell)}</td>" for cell in row)
                rows.append(f"<tr>{cells}</tr>")
            parts.append(f"<table><tbody>{''.join(rows)}</tbody></table>")
        elif kind == "rule":
            parts.append("<hr />")
    return "".join(parts) or f"<p>{html.escape(markdown or '')}</p>"
