"""Tell the model what day it is, on every model call.

The pm agent answers questions like "summarise today's standup" and files
capture pages under date folders, but a model has no clock: without an
explicit timestamp it infers "today" from whatever dates appear in tool
results. That is how a Friday recording got summarised as "Today's Standup"
the following Tuesday — the newest recording was from 31 July, so the model
concluded today was 31 July.

The stamp is appended per model call (not baked into the system prompt at
graph build) so long-running threads and queued runs always see the current
moment, in the team's timezone.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

# The team operates on German time; override per deployment if that changes.
_DEFAULT_TIMEZONE = "Europe/Berlin"


def current_time_stamp(timezone: str | None = None) -> str:
    tz = ZoneInfo(timezone or os.environ.get("LOUPFEED_TIMEZONE") or _DEFAULT_TIMEZONE)
    now = datetime.now(tz)
    return (
        f"\n\n### Current date and time\n"
        f"It is now {now.strftime('%A, %d %B %Y, %H:%M')} ({tz.key}). "
        f"Resolve every relative date ('today', 'yesterday', 'this week') against this, "
        f"never against dates found in tool results. If the newest recording, message or "
        f"document you find is older than the day being asked about, say which day it is "
        f"actually from instead of presenting it as the requested day."
    )


def _stamped(request: ModelRequest) -> ModelRequest:
    base = request.system_message.text if request.system_message is not None else ""
    return request.override(system_message=SystemMessage(content=base + current_time_stamp()))


class CurrentTimeMiddleware(AgentMiddleware):
    """Append a fresh timestamp to the system prompt on every model call."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(_stamped(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> Any:
        return await handler(_stamped(request))
