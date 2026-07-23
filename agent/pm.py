"""pm graph — loupfeedpm, the product-management agent.

A lean, sandbox-less deep agent (same shape as ``agent.chat``): a tool loop
over the platform's MCP connectors (Atlassian Rovo first — Jira, Confluence),
web tools, and read-only GitHub access. Conversations arrive thread-scoped
(one surface thread ⇄ one LangGraph thread); multi-user transcripts carry
speaker labels supplied by the entry adapter.

Org-specific guidance (site URLs, workflow conventions like idea → planning →
dev hierarchies) is appended from ``PM_PROMPT_EXTRA_FILE`` (default
``/etc/loupfeed/pm-prompt.md``) — the platform prompt stays org-agnostic.
"""
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import logging
import os
import warnings
from pathlib import Path

from langgraph.graph.state import RunnableConfig
from langgraph.pregel import Pregel

warnings.filterwarnings("ignore", module="langchain_core._api.deprecation")
warnings.filterwarnings("ignore", message=".*Pydantic V1.*", category=UserWarning)

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelCallLimitMiddleware

from .dashboard.options import SUPPORTED_MODEL_IDS, model_supports_effort
from .dashboard.team_settings import get_team_default_model
from .dashboard.user_mappings import login_for_email
from .middleware import (
    ExcludeToolsMiddleware,
    SanitizeThinkingBlocksMiddleware,
    SanitizeToolInputsMiddleware,
    ToolErrorMiddleware,
)
from .connector_auth import connection_status
from .pm_connectors import load_connector_tools
from .server import (
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_RECURSION_LIMIT,
    graph_loaded_for_execution,
)
from .tools import (
    fetch_url,
    http_request,
    read_repo_file,
    search_repo_code,
    web_search,
)
from .utils.github_app import get_github_app_installation_token
from .utils.model import DEFAULT_LLM_REASONING, make_model, provider_model_kwargs
from .utils.tracing import PM_TRACING_PROJECT, traced_graph_factory

logger = logging.getLogger(__name__)

PM_MODEL_CALL_LIMIT = 100

# No sandbox: strip the filesystem-write/execute tools deepagents injects.
_EXCLUDED_TOOLS = frozenset({"execute", "write_file", "edit_file"})

PM_PROMPT = """You are **loupfeedpm**, the product-management agent of the loupfeed agents \
platform. People mention you in conversation threads (team chats, channels, meeting chats) \
to ask questions about — and act on — their planning system.

You have NO sandbox and cannot run code. You work through your tools.

Conversations are thread-scoped: one conversation thread maps to one of your threads, and \
several people may talk into the same thread. Messages may carry speaker labels \
("Name: ..."); track who said what, and address people by name when useful.

Tools:
- **Planning-system tools** — loaded from whichever MCP connectors your workspace has \
connected (Jira/Confluence via Atlassian today; more connectors over time). Tool names and \
schemas come from the connector — read them.

Connector state right now:
{connector_status}
- `web_search`, `fetch_url`, `http_request` — external docs and APIs.
- `read_repo_file`, `search_repo_code` — read-only access to the thread's bound GitHub \
repository, when one is configured for this thread.

Working rules:
- **Search before you create.** Before creating any issue or page, search for existing \
items covering the same ground and say what you found. Prefer enriching an existing item \
(comment + description update) over creating a near-duplicate.
- **Report what you did with keys and links.** When you create or change something, reply \
with the issue key / page title and its URL.
- **Triage ideas properly.** When asked to triage an idea from a discussion: extract the \
core idea (problem, proposal, decisions, open questions), dedupe against the existing \
backlog, then create or enrich the right item — and cite where the idea came from.
- **Personal queries need the right person.** For "my issues"-style questions, resolve the \
requester (from speaker labels or thread context) and scope queries to them.
- **Stay inside the thread's register.** Replies are chat messages: short, skimmable, \
leading with the answer or the action taken. No report formatting unless asked.
- **If a task needs a connector that is NOT CONNECTED, say so briefly and share its \
connect link** (an admin connects it once for the whole workspace) — then help as far as \
conversation context allows. Never pretend to have access you don't have, and never invent \
issue keys, people, links, or decisions.
"""

# Deployment-specific guidance (site URLs, workflow conventions, team norms).
_PM_PROMPT_EXTRA_FILE = os.environ.get("PM_PROMPT_EXTRA_FILE", "/etc/loupfeed/pm-prompt.md")


def _prompt_extras() -> str:
    try:
        extra = Path(_PM_PROMPT_EXTRA_FILE).read_text().strip()
    except OSError:
        return ""
    return f"\n\n---\n\n### Workspace conventions\n\n{extra}" if extra else ""


async def _resolve_pm_model(configurable: dict) -> tuple[str, str]:
    model_id = configurable.get("pm_model_id")
    effort = configurable.get("pm_effort")
    if (
        isinstance(model_id, str)
        and model_id in SUPPORTED_MODEL_IDS
        and isinstance(effort, str)
        and model_supports_effort(model_id, effort)
    ):
        return model_id, effort
    return await get_team_default_model("agent")


async def get_pm_agent(config: RunnableConfig) -> Pregel:
    """Get the pm agent. No sandbox; connector tools loaded per run."""
    thread_id = config["configurable"].get("thread_id")
    config["recursion_limit"] = DEFAULT_RECURSION_LIMIT

    if thread_id is None or not graph_loaded_for_execution(config):
        return create_deep_agent(system_prompt="", tools=[]).with_config(config)

    configurable = config["configurable"]

    # Read-only repo tools authenticate with a repo-scoped App token, resolved
    # in-graph (same pattern as the chat graph). The adapter binds a repo to the
    # thread via chat_repo_owner / chat_repo_name in configurable.
    repo_name = str(configurable.get("chat_repo_name") or "")
    try:
        token = await get_github_app_installation_token(
            repositories=[repo_name] if repo_name else None
        )
        if isinstance(token, str) and token:
            configurable["chat_github_token"] = token
    except Exception:
        logger.exception("pm: GitHub App token unavailable; repo tools will be inert")

    connector_tools = await load_connector_tools()

    model_id, effort = await _resolve_pm_model(configurable)
    model_kwargs = provider_model_kwargs(
        model_id,
        effort,
        max_tokens=DEFAULT_LLM_MAX_TOKENS,
        openai_reasoning_default=DEFAULT_LLM_REASONING,
    )

    status = await asyncio.to_thread(connection_status)
    base = os.environ.get("CONNECTOR_PUBLIC_BASE_URL", "").rstrip("/")
    lines = []
    for name, st in sorted(status.items()):
        if st.get("connected"):
            lines.append(f"- {name}: CONNECTED")
        else:
            link = f"{base}/connectors/{name}/start" if base else "(no public URL configured)"
            lines.append(f"- {name}: NOT CONNECTED — connect link: {link}")
    connector_status = "\n".join(lines) if lines else "- (no connectors registered)"

    requester_block = ""
    requester_name = str(configurable.get("requester_name") or "")
    requester_email = str(configurable.get("requester_email") or "")
    if requester_name or requester_email:
        github_login = None
        if requester_email:
            try:
                github_login = await login_for_email(requester_email)
            except Exception:
                logger.warning("pm: user-mapping lookup failed", exc_info=True)
        parts = [p for p in (requester_name, f"<{requester_email}>" if requester_email else "") if p]
        line = " ".join(parts)
        if github_login:
            line += f" · GitHub: {github_login}"
        requester_block = (
            "\n\n### Requester\n"
            f"The person who sent the latest message: {line}\n"
            "Use this identity for personal queries (\"my issues\", \"assign to me\") and "
            "for attributing actions in comments you write."
        )

    system_prompt = (
        PM_PROMPT.format(connector_status=connector_status) + requester_block + _prompt_extras()
    )

    return create_deep_agent(
        model=make_model(model_id, **model_kwargs),
        system_prompt=system_prompt,
        tools=[
            *connector_tools,
            web_search,
            fetch_url,
            http_request,
            read_repo_file,
            search_repo_code,
        ],
        middleware=[
            SanitizeToolInputsMiddleware(),
            ModelCallLimitMiddleware(run_limit=PM_MODEL_CALL_LIMIT, exit_behavior="end"),
            ToolErrorMiddleware(),
            ExcludeToolsMiddleware(excluded=_EXCLUDED_TOOLS),
            SanitizeThinkingBlocksMiddleware(),
        ],
    ).with_config(config)


traced_pm_agent = traced_graph_factory(get_pm_agent, PM_TRACING_PROJECT)
