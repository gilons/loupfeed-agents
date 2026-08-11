"""Agent replies must arrive formatted, not as one flat paragraph of markdown.

Every reply the platform posted went out as a single plain-text node, so a real
triage report rendered as literal `**Verdict**` and backticks in one run-on
block (seen live on SPB-7). These tests pin the conversion for both products.
"""

from __future__ import annotations

from agent.utils.markdown_reply import markdown_to_adf, markdown_to_storage

# The opening of the report that rendered wrong live.
REPORT = """**Verdict** — confirmed, high confidence. Search-based.

**Where** — `dinolabdev/deliveru`, `apps/webapp/app/x.tsx`, the `RadioGroup` element.

**Suspect commits**

- **`b98e59f`** (Mr Break, 2026-07-20) — the introducing commit. PR #2401.
- **`73a4ec4`** (EwiJosepha, 2026-08-06) — the fix commit.
"""


def _types(doc):
    return [block["type"] for block in doc["content"]]


def test_the_report_becomes_paragraphs_and_a_list_not_one_text_node():
    doc = markdown_to_adf(REPORT)
    assert _types(doc) == ["paragraph", "paragraph", "paragraph", "bulletList"]
    assert len(doc["content"][3]["content"]) == 2


def test_bold_and_inline_code_become_marks_rather_than_literal_characters():
    doc = markdown_to_adf("**Verdict** — see `apps/webapp/app/x.tsx` now")
    nodes = doc["content"][0]["content"]
    bold = next(n for n in nodes if n.get("marks") == [{"type": "strong"}])
    code = next(n for n in nodes if n.get("marks") == [{"type": "code"}])
    assert bold["text"] == "Verdict"
    assert code["text"] == "apps/webapp/app/x.tsx"
    # The delimiters themselves must be gone from every text node.
    assert not any("**" in n["text"] or "`" in n["text"] for n in nodes)


def test_bold_wrapping_inline_code_keeps_both_marks():
    """The agents write ``**`abc1234`**`` constantly.

    Treating the bold body as literal text left the backticks visible in the
    posted comment, which is what Jira actually stored on the first live run.
    """
    doc = markdown_to_adf("- **`b98e59f`** (Mr Break) — the introducing commit")
    item = doc["content"][0]["content"][0]["content"][0]["content"]
    sha = item[0]
    assert sha["text"] == "b98e59f"
    # ADF's code mark excludes strong: sending both makes Jira reject the whole
    # comment with 400 INVALID_INPUT, which it did live.
    assert {m["type"] for m in sha["marks"]} == {"code"}
    assert not any("`" in node["text"] for node in item)


def test_code_is_never_emitted_alongside_marks_adf_forbids():
    doc = markdown_to_adf("**bold** and *italic* and **`code`** and [`linked`](https://x.dev)")
    for block in doc["content"]:
        for node in block["content"]:
            types = {m["type"] for m in node.get("marks") or []}
            if "code" in types:
                assert types <= {"code", "link"}, f"ADF rejects {types}"


def test_storage_format_also_nests_bold_and_code():
    out = markdown_to_storage("**`b98e59f`** did it")
    assert "<strong><code>b98e59f</code></strong>" in out
    assert "`" not in out


def test_headings_rules_and_code_blocks_survive():
    doc = markdown_to_adf("### Findings\n\n---\n\n```python\nx = 1\n```")
    assert _types(doc) == ["heading", "rule", "codeBlock"]
    assert doc["content"][0]["attrs"]["level"] == 3
    assert doc["content"][2]["attrs"]["language"] == "python"
    assert doc["content"][2]["content"][0]["text"] == "x = 1"


def test_numbered_lists_stay_numbered():
    doc = markdown_to_adf("1. read the diff\n2. blame the line")
    assert _types(doc) == ["orderedList"]
    assert len(doc["content"][0]["content"]) == 2


def test_a_link_becomes_a_link_mark():
    doc = markdown_to_adf("see [PR 2401](https://github.com/acme/x/pull/2401) for context")
    marks = [n.get("marks") for n in doc["content"][0]["content"] if n.get("marks")]
    assert marks == [[{"type": "link", "attrs": {"href": "https://github.com/acme/x/pull/2401"}}]]


def test_a_list_item_continued_on_the_next_line_stays_one_item():
    doc = markdown_to_adf("- first part\n  continued here\n- second")
    items = doc["content"][0]["content"]
    assert len(items) == 2
    assert "continued here" in items[0]["content"][0]["content"][0]["text"]


def test_plain_prose_is_still_a_paragraph():
    doc = markdown_to_adf("Just a sentence.")
    assert _types(doc) == ["paragraph"]
    assert doc["content"][0]["content"][0]["text"] == "Just a sentence."


def test_empty_input_never_produces_an_invalid_document():
    for value in ("", "   ", None):
        doc = markdown_to_adf(value)  # type: ignore[arg-type]
        assert doc["type"] == "doc" and doc["content"]


# --- Confluence storage format --------------------------------------------


def test_storage_format_gets_real_markup():
    out = markdown_to_storage(REPORT)
    assert "<strong>Verdict</strong>" in out
    assert "<code>dinolabdev/deliveru</code>" in out
    assert out.count("<li>") == 2
    assert "**" not in out


def test_storage_format_escapes_html_so_a_reply_cannot_break_the_page():
    """The old `<p>{text}</p>` would have injected this verbatim."""
    out = markdown_to_storage("compare a < b && c > d <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;&amp;" in out


def test_storage_code_block_uses_the_code_macro():
    out = markdown_to_storage("```\nrm -rf /\n```")
    assert 'ac:name="code"' in out
    assert "<![CDATA[rm -rf /]]>" in out


def test_storage_renders_a_table_with_a_header():
    md = "| Region | What it holds |\n|---|---|\n| Queue | Every **conversation** |\n"
    out = markdown_to_storage(md)
    assert "<table><tbody>" in out
    assert "<th>Region</th><th>What it holds</th>" in out
    assert "<td>Queue</td><td>Every <strong>conversation</strong></td>" in out
    assert "|" not in out


def test_adf_renders_a_table_with_a_header():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
    doc = markdown_to_adf(md)
    table = doc["content"][0]
    assert table["type"] == "table"
    assert [cell["type"] for cell in table["content"][0]["content"]] == [
        "tableHeader",
        "tableHeader",
    ]
    assert [cell["type"] for cell in table["content"][1]["content"]] == [
        "tableCell",
        "tableCell",
    ]


def test_headerless_table_still_renders_as_rows():
    out = markdown_to_storage("| a | b |\n| c | d |")
    assert "<th>" not in out
    assert out.count("<tr>") == 2


def test_a_table_between_paragraphs_keeps_both():
    out = markdown_to_storage("Before\n\n| A |\n|---|\n| 1 |\n\nAfter")
    assert out.startswith("<p>Before</p><table>")
    assert out.endswith("<p>After</p>")
