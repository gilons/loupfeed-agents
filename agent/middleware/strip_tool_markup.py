"""Strip leaked tool-call markup from model output.

When a model reaches for a tool it does not have, some providers emit the
tool-call syntax as plain text instead of a structured call. It then flows
straight into whatever the agent posts — a Teams reply, a Jira comment — and
the reader sees raw markup like ``<|DSML|tool_calls>…</|DSML|tool_calls>``.

Observed in production: the pm agent tried to ``write_file`` (a tool it is
deliberately not given) and the whole attempted call landed in a Teams message.

This strips those fragments from message text. It cannot make the model stop
inventing tools, but it keeps the artefact out of the reader's face.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse

logger = logging.getLogger(__name__)

# Whole blocks first, then any stragglers left by a truncated emission.
_PATTERNS = (
    re.compile(
        r"<\s*\|?\s*DSML\s*\|?.*?tool_calls\s*>.*?<\s*/?\s*\|?\s*DSML\s*\|?.*?tool_calls\s*>",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"</?\s*\|?\s*DSML\s*\|[^>]*>", re.IGNORECASE),
    re.compile(r"</?\s*(?:antml:)?(?:invoke|parameter|tool_calls)\b[^>]*>", re.IGNORECASE),
)


def strip_tool_markup(text: str) -> str:
    """Remove leaked tool-call markup, leaving the surrounding prose intact."""
    cleaned = text
    for pattern in _PATTERNS:
        cleaned = pattern.sub("", cleaned)
    # Collapse the blank space the removal leaves behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _sanitize(message: Any) -> None:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        cleaned = strip_tool_markup(content)
        if cleaned != content:
            logger.warning("stripped leaked tool-call markup from a model reply")
            message.content = cleaned
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                cleaned = strip_tool_markup(block["text"])
                if cleaned != block["text"]:
                    logger.warning("stripped leaked tool-call markup from a model reply")
                    block["text"] = cleaned


def _sanitize_response(response: Any) -> Any:
    for message in getattr(response, "result", None) or []:
        _sanitize(message)
    return response


class StripToolMarkupMiddleware(AgentMiddleware):
    """Keep provider tool-call syntax out of anything the agent says."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return _sanitize_response(handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> Any:
        return _sanitize_response(await handler(request))
