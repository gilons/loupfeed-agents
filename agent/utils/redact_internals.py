"""Keep infrastructure detail out of anything a user reads.

The prompt tells the agent not to quote permission names, endpoints or status
codes. It does anyway — repeatedly — because the detail is sitting in its own
conversation history: a tool once returned a raw Graph 403 body, that
``ToolMessage`` is now permanent thread state, and the model keeps re-reading
and re-explaining it. Four standups in a row were answered with "an admin needs
to grant Chat.Read.WhereInstalled".

Instruction cannot fix state. So this redacts at both ends:

- **Ingress** — tool failures are summarised before they enter the transcript,
  so nothing accumulates for the model to quote later.
- **Egress** — anything about to be posted is scrubbed, whatever the model
  decided to write.

The point is not secrecy. It is that these strings are useless to the reader and
actively harmful: they sent a colleague chasing a tenant-wide permission grant
that would not have fixed the problem.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_REPLACEMENT = "an internal access limit"

# Microsoft Graph / Entra permission names: Chat.Read.WhereInstalled,
# ChannelMessage.Read.Group, OnlineMeetingTranscript.Read.All, Sites.Read.All …
_PERMISSION = re.compile(
    r"\b(?:[A-Z][A-Za-z]+){1,4}\.(?:Read|ReadWrite|ReadBasic|Create|Send|Selected)"
    r"(?:\.(?:All|Group|Chat|Team|WhereInstalled|Selected))?\b"
)
_GRAPH_PATH = re.compile(
    r"(?<![\w/])/(?:v1\.0|beta)?/?(?:chats|teams|groups|users|sites|drives)/[^\s`,)\]]+"
)
_HTTP_STATUS = re.compile(
    r"\b(?:HTTP\s*)?(?:40[0-9]|41[0-9]|42[0-9]|50[0-9])\b(?=\s*(?:error|response|status|—|-|:|\.|,|\)|$))",
    re.IGNORECASE,
)
_ENV_VAR = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,}\b")
_ADMIN_SURFACE = re.compile(r"\b(?:Azure AD|Entra ID|Entra|Microsoft Entra)\b", re.IGNORECASE)

_TOOL_NAMES = (
    "graph_meeting_transcript", "graph_find_recording", "graph_file_content",
    "transcribe_channel_meeting", "transcribe_recording", "confluence_attach_image",
    "graph_api", "github_api", "read_repo_file", "search_repo_code",
)  # fmt: skip
_TOOL_NAME = re.compile(r"`?\b(?:" + "|".join(_TOOL_NAMES) + r")\b`?")


def redact_internals(text: str) -> str:
    """Replace infrastructure detail with plain language.

    Leaves ordinary prose — including Jira keys and product names — untouched.
    """
    if not text:
        return text
    original = text
    text = _PERMISSION.sub(_REPLACEMENT, text)
    text = _GRAPH_PATH.sub("an internal endpoint", text)
    text = _ENV_VAR.sub("a configuration value", text)
    text = _TOOL_NAME.sub("one of my tools", text)
    text = _ADMIN_SURFACE.sub("the admin console", text)
    text = _HTTP_STATUS.sub("an error", text)
    # Collapse the repetition the substitutions can create.
    text = re.sub(
        rf"(?:{re.escape(_REPLACEMENT)}[,/ ]+){{1,}}{re.escape(_REPLACEMENT)}", _REPLACEMENT, text
    )
    text = re.sub(r"[ \t]{2,}", " ", text)
    if text != original:
        logger.info("redacted infrastructure detail from an outbound message")
    return text


def summarise_tool_failure(status: int, body: str, *, what: str) -> str:
    """A short, quotable reason for a failed call — the detail goes to the log.

    Used instead of handing a raw error body to the model, so the transcript
    never accumulates strings it will later repeat to a human.
    """
    logger.warning("%s failed (%s): %s", what, status, body[:500])
    if status in (401, 403):
        return f"I don't have access to {what}."
    if status == 404:
        return f"I couldn't find {what}."
    if status == 429:
        return f"I'm being rate-limited reading {what} — worth retrying shortly."
    return f"I couldn't read {what} just now."
