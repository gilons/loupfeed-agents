"""Infrastructure detail must never reach a reader.

Every REAL string here was posted to a Teams thread by the agent. The prompt
already forbade all of it.
"""

from __future__ import annotations

from agent.utils.redact_internals import redact_internals, summarise_tool_failure

# Posted verbatim, four standups running.
FOUR_STANDUPS = (
    "Giles, I'm going to be direct: I've now been blocked on four consecutive standups "
    "(July 28, 29, 30, 31) by the same Chat.Read.WhereInstalled permission.\n"
    "The fix is one Azure AD permission grant: Chat.Read.WhereInstalled on the loupfeed app."
)

PERMISSION_DUMP = (
    "The app is installed with `Sites.Read.All`, `User.Read.All`, "
    "`OnlineMeetingTranscript.Read.All`, and `Group.Selected` but that doesn't cover "
    "chat-level reads. An admin needs to grant `Chat.Read.WhereInstalled` and "
    "`ChatMessage.Read.Chat` (or broader `Chat.Read.All`)."
)


def test_permission_names_never_survive():
    out = redact_internals(FOUR_STANDUPS)
    for leaked in ("Chat.Read.WhereInstalled", "Azure AD"):
        assert leaked not in out, out


def test_every_permission_in_a_dump_is_replaced():
    out = redact_internals(PERMISSION_DUMP)
    for leaked in (
        "Sites.Read.All",
        "User.Read.All",
        "OnlineMeetingTranscript.Read.All",
        "Group.Selected",
        "Chat.Read.WhereInstalled",
        "ChatMessage.Read.Chat",
        "Chat.Read.All",
    ):
        assert leaked not in out, f"{leaked} survived: {out}"


def test_tool_names_and_endpoints_are_replaced():
    text = (
        "The `graph_meeting_transcript` tool needs to resolve the chat first, and "
        "/chats/19:abc@thread.tacv2/messages returned 403."
    )
    out = redact_internals(text)
    assert "graph_meeting_transcript" not in out
    assert "/chats/19:abc@thread.tacv2/messages" not in out
    assert "403" not in out


def test_status_codes_are_caught_mid_sentence():
    """'returned 403 on ...' slipped through the first version of this."""
    out = redact_internals("graph_meeting_transcript returned 403 on the meeting chat.")
    assert "403" not in out, out


def test_quantities_are_not_mistaken_for_status_codes():
    for text in (
        "We imported 500 candidates yesterday.",
        "There are 404 open items in the backlog.",
        "The exposé run covered 429 documents.",
    ):
        assert redact_internals(text) == text, text


def test_config_key_names_are_replaced():
    out = redact_internals("Set ASSEMBLY_AI_API_KEY and TEAMS_APP_TENANT_ID first.")
    assert "ASSEMBLY_AI_API_KEY" not in out
    assert "TEAMS_APP_TENANT_ID" not in out


def test_ordinary_answers_are_untouched():
    for text in (
        "I filed SPI-19 and linked it to the capture page.",
        "Ewi finished the exposé fix; Gael is deploying Storybook to stories.deliveru.dev.",
        "The standup ran 12 minutes with 4 speakers. Want me to write it up?",
        "SPD-2 is Ready and assigned to Gael.",
    ):
        assert redact_internals(text) == text, text


def test_failure_summary_is_short_and_clean():
    reason = summarise_tool_failure(
        403,
        "{'error': {'message': \"Missing role permissions... 'Chat.Read.WhereInstalled'...\"}}",
        what="that meeting's transcript",
    )
    assert reason == "I don't have access to that meeting's transcript."
    assert "Chat" not in reason
    assert "403" not in reason


def test_empty_text_is_safe():
    assert redact_internals("") == ""
