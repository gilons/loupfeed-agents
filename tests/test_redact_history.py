"""History already in a thread must be neutralised before the model reads it.

The fixtures are the real messages recovered from the poisoned standup thread:
a raw Graph 403 body recorded as a tool result, and the agent's own reply that
was generated from it.
"""

from __future__ import annotations

from agent.middleware.redact_history import redact_history

POISONED_TOOL_RESULT = (
    '{"ok": false, "reason": "could not read chat (403): {\\"error\\":{\\"code\\":\\"Forbidden\\",'
    '\\"message\\":\\"Missing role permissions on the request. API requires one of '
    "'Chat.ReadBasic.WhereInstalled, Chat.Read.WhereInstalled, Chat.ReadWrite.WhereInstalled, "
    "Chat.Read.All'. Roles on the request 'Sites.Read.All, User.Read.All, "
    'OnlineMeetingTranscript.Read.All, Group.Selected\'\\"}}"}'
)

POISONED_AI_REPLY = (
    "The fix is an admin action — grant `Chat.Read.WhereInstalled` to the loupfeed Teams "
    "app in Azure AD. Until then I can't resolve the chat through graph_meeting_transcript."
)


class _Msg:
    def __init__(self, type_: str, content):
        self.type = type_
        self.content = content


def test_tool_results_are_cleaned():
    msg = _Msg("tool", POISONED_TOOL_RESULT)
    assert redact_history([msg]) == 1
    for leaked in ("Chat.Read.WhereInstalled", "Sites.Read.All", "Group.Selected"):
        assert leaked not in msg.content, msg.content


def test_the_agents_own_past_replies_are_cleaned():
    msg = _Msg("ai", POISONED_AI_REPLY)
    assert redact_history([msg]) == 1
    assert "Chat.Read.WhereInstalled" not in msg.content
    assert "Azure AD" not in msg.content
    assert "graph_meeting_transcript" not in msg.content


def test_human_messages_are_never_rewritten():
    """People's own words stay verbatim, even when they mention internals."""
    said = "Should we just grant Chat.Read.All and be done with it?"
    msg = _Msg("human", said)
    assert redact_history([msg]) == 0
    assert msg.content == said


def test_clean_history_is_left_alone():
    msgs = [
        _Msg("ai", "I filed SPI-19 and linked it to the capture page."),
        _Msg("tool", '{"ok": true, "transcript": "[00:02] Speaker A: Morning."}'),
    ]
    assert redact_history(msgs) == 0


def test_block_style_content_is_handled():
    msg = _Msg("ai", [{"type": "text", "text": POISONED_AI_REPLY}, {"type": "text", "text": "ok"}])
    assert redact_history([msg]) == 1
    assert "Chat.Read.WhereInstalled" not in msg.content[0]["text"]
    assert msg.content[1]["text"] == "ok"
