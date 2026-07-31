"""Redact infrastructure detail out of conversation history before the model reads it.

Sanitising new tool results stops the leak growing, but threads already hold
raw Graph error bodies — permission names, endpoints, status codes — recorded
before that fix. The model re-reads them every turn and keeps re-explaining
them, which is how four standups in a row were answered with "an admin needs to
grant Chat.Read.WhereInstalled". Redacting only outbound messages hides the
symptom; the model still *reasons* from the poisoned history and steers the
conversation toward the wrong fix.

So the history is cleaned on the way in: prior tool results and the agent's own
past messages are redacted per model call. Nothing is deleted and no thread is
reset — the substance of the conversation survives, only the infrastructure
noise goes. Human messages are left exactly as written.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse

from ..utils.redact_internals import redact_internals

logger = logging.getLogger(__name__)

# Only what the machine produced. A human's own words are never rewritten.
_REDACTED_TYPES = ("tool", "ai")


def _message_type(message: Any) -> str:
    return str(getattr(message, "type", "") or "").lower()


def _redact_message(message: Any) -> bool:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        cleaned = redact_internals(content)
        if cleaned != content:
            message.content = cleaned
            return True
        return False
    changed = False
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                cleaned = redact_internals(block["text"])
                if cleaned != block["text"]:
                    block["text"] = cleaned
                    changed = True
    return changed


def redact_history(messages: list[Any]) -> int:
    """Redact tool results and prior agent messages in place; return how many changed."""
    changed = 0
    for message in messages:
        if _message_type(message) in _REDACTED_TYPES and _redact_message(message):
            changed += 1
    return changed


class RedactHistoryMiddleware(AgentMiddleware):
    """Neutralise infrastructure detail already sitting in a thread."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        changed = redact_history(request.messages)
        if changed:
            logger.info("redacted infrastructure detail from %d message(s) of history", changed)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> Any:
        changed = redact_history(request.messages)
        if changed:
            logger.info("redacted infrastructure detail from %d message(s) of history", changed)
        return await handler(request)
