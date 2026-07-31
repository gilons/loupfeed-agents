"""Tests for stripping leaked tool-call markup out of replies.

The trigger case is real: the pm agent reached for ``write_file`` (a tool it is
deliberately not given) and the provider emitted the call as plain text, which
landed in a Teams message.
"""

from __future__ import annotations

from agent.middleware.strip_tool_markup import _sanitize, strip_tool_markup

LEAKED = (
    "Let me try a different approach — write the PNG bytes to disk and use the "
    "Confluence attachment endpoint.\n"
    '< | DSML | tool_calls>< | DSML | invoke name="write_file">< | DSML | parameter '
    'name="file_path" string="true">/image1.png</ | DSML | parameter>'
    "</ | DSML | invoke></ | DSML | tool_calls>"
)


def test_strips_the_leaked_block_and_keeps_the_prose():
    cleaned = strip_tool_markup(LEAKED)
    assert cleaned.startswith("Let me try a different approach")
    assert "DSML" not in cleaned
    assert "write_file" not in cleaned
    assert "tool_calls" not in cleaned


def test_leaves_ordinary_text_untouched():
    text = "I filed SPI-19 and linked it to the capture page. Anything else?"
    assert strip_tool_markup(text) == text


def test_does_not_eat_legitimate_angle_brackets():
    text = "The check is `if a < b and b > c:` — that stays."
    assert strip_tool_markup(text) == text


def test_sanitizes_string_message_content():
    class _Msg:
        content = LEAKED

    msg = _Msg()
    _sanitize(msg)
    assert "DSML" not in msg.content


def test_sanitizes_block_content():
    class _Msg:
        content = [{"type": "text", "text": LEAKED}, {"type": "text", "text": "fine"}]

    msg = _Msg()
    _sanitize(msg)
    assert "DSML" not in msg.content[0]["text"]
    assert msg.content[1]["text"] == "fine"
