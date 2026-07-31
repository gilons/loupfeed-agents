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

from .connector_auth import connection_status
from .dashboard.options import SUPPORTED_MODEL_IDS, model_supports_effort
from .dashboard.team_settings import get_team_default_model
from .dashboard.user_mappings import login_for_email
from .middleware import (
    ExcludeToolsMiddleware,
    RedactHistoryMiddleware,
    SanitizeThinkingBlocksMiddleware,
    SanitizeToolInputsMiddleware,
    StripToolMarkupMiddleware,
    ToolErrorMiddleware,
)
from .pm_connectors import load_connector_tools
from .server import (
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_RECURSION_LIMIT,
    graph_loaded_for_execution,
)
from .tools import (
    confluence_attach_image,
    fetch_url,
    github_api,
    graph_api,
    graph_file_content,
    graph_find_recording,
    graph_meeting_transcript,
    http_request,
    read_repo_file,
    search_repo_code,
    transcribe_channel_meeting,
    transcribe_recording,
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
- `github_api` — read-only GitHub REST access **authenticated as the org's GitHub App** \
(sees private repos, org membership, issues, PRs, commits). For ANY GitHub question — \
membership, usernames, repos, activity — use this tool; never guess or web-search GitHub \
facts.
- `graph_api` — read-only Microsoft Graph (Microsoft 365) access as this Teams app. \
Reads messages, members, meeting details, and files in the teams/chats/meetings where \
the app is installed, plus directory (people, org chart) and SharePoint reads the \
tenant admin consented to. Use it to pull real Microsoft 365 context instead of asking \
people to paste it.
- `graph_meeting_transcript` — the transcript of the meeting behind a meeting chat \
(pass the conversation id from the Teams context section, when present).
- `transcribe_channel_meeting` — for meetings held in a CHANNEL (standups, reviews), \
the one-step way to get what was said: it finds the recording and transcribes it. Use this \
rather than fetching a link in one turn and transcribing in the next — recording links \
expire within minutes. `graph_find_recording` + `transcribe_recording` remain for \
(standups, reviews), Teams' own transcript is not reachable: find the recording in the \
channel's Recordings folder, then transcribe it. Speaker turns come back unnamed, so \
cross-reference the call's participant list before attributing anything.
- `graph_file_content` — the readable text of a file shared in Teams/SharePoint \
(PDF, DOCX, plain-text). Pass an attachment's `contentUrl` from a message, or a \
drive/item id pair from a file listing. Use it to actually read shared documents \
before triaging them.
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
- **Ground triage in what you actually read.** When triaging a meeting or conversation, \
first read the real material (transcript via `graph_meeting_transcript`, messages and \
shared files via `graph_api`) and quote or reference the specific statements and \
decisions each idea came from. Never invent transcript content; if you couldn't read \
something, say so.
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
        parts = [
            p for p in (requester_name, f"<{requester_email}>" if requester_email else "") if p
        ]
        line = " ".join(parts)
        if github_login:
            line += f" · GitHub: {github_login}"
        requester_block = (
            "\n\n### Requester\n"
            f"The person who sent the latest message: {line}\n"
            'Use this identity for personal queries ("my issues", "assign to me") and '
            "for attributing actions in comments you write."
        )

    teams_block = ""
    conversation_id = str(configurable.get("teams_conversation_id") or "")
    if conversation_id:
        conversation_type = str(configurable.get("teams_conversation_type") or "")
        lines = [
            f"- conversation id (use with `graph_api` / `graph_meeting_transcript`): "
            f"`{conversation_id}`",
            f"- conversation type: {conversation_type or 'unknown'}",
        ]
        team_group_id = str(configurable.get("teams_team_group_id") or "")
        if team_group_id:
            lines.append(f"- team group id (use as `{{team-group-id}}`): `{team_group_id}`")
        if configurable.get("teams_meeting_id") or conversation_id.startswith("19:meeting_"):
            lines.append(
                "- this is a MEETING chat: `graph_meeting_transcript` with the conversation "
                "id above reads the call transcript; `/chats/{id}/messages` reads the "
                "meeting chat."
            )
        teams_block = (
            "\n\n### Teams context\nThis conversation comes from Microsoft Teams:\n"
            + "\n".join(lines)
        )

    system_prompt = (
        PM_PROMPT.format(connector_status=connector_status)
        + requester_block
        + teams_block
        + _prompt_extras()
    )

    return create_deep_agent(
        model=make_model(model_id, **model_kwargs),
        system_prompt=system_prompt,
        tools=[
            *connector_tools,
            github_api,
            graph_api,
            graph_file_content,
            graph_meeting_transcript,
            graph_find_recording,
            transcribe_channel_meeting,
            transcribe_recording,
            confluence_attach_image,
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
            RedactHistoryMiddleware(),
            StripToolMarkupMiddleware(),
        ],
    ).with_config(config)


traced_pm_agent = traced_graph_factory(get_pm_agent, PM_TRACING_PROJECT)
