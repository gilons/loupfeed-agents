"""triage graph — the bug agent: what is broken, where, and which commit did it.

Read-only by construction. It has no sandbox, cannot run the product, and
cannot write to a repository; its output is a report a developer can act on.
Fixing is the coding agent's job, and handing a bug over is a human's decision.

The shape of the work comes from one fact about loupfeed releases: they are
``<surface>@<commit>``, so an anchored report already names the exact tree the
reporter was running. Pinning a culprit is then a join (release -> sha,
manifest -> path:line, blame at that sha) rather than a guess. Reports that
arrive as prose have no such anchor, so the agent looks for the anchored twin
first and falls back to search, saying which one it did.

Repository knowledge is deployment data and lives in the surface registry
(``agent.surfaces``), so nothing here names a product.
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
from .middleware import (
    CurrentTimeMiddleware,
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
from .surfaces import load_surfaces, registry_summary, surface_for_key
from .tools import (
    fetch_url,
    find_prior_triage,
    git_blame_line,
    git_commit_diff,
    git_commits_touching,
    git_compare,
    github_api,
    loupfeed_find_reports,
    loupfeed_report,
    read_repo_file,
    record_triage,
    search_repo_code,
    web_search,
)
from .utils.github_app import get_github_app_installation_token
from .utils.model import DEFAULT_LLM_REASONING, make_model, provider_model_kwargs
from .utils.tracing import PM_TRACING_PROJECT, traced_graph_factory

logger = logging.getLogger(__name__)

TRIAGE_MODEL_CALL_LIMIT = 120

# No sandbox and no writes: strip everything deepagents would give us for
# changing files. Triage that edits code is not triage.
_EXCLUDED_TOOLS = frozenset({"execute", "write_file", "edit_file"})

TRIAGE_PROMPT = """You are **loupfeedtriage**, the bug agent of the loupfeed agents platform. \
A bug report reaches you and you answer three questions: is it real, where does it live, and \
which change most likely caused it. You do NOT fix it. A developer reads your report and takes \
it from there.

You have NO sandbox. You cannot run the product, reproduce a bug by using it, or change any \
code. You work from reports, from git history, and from reading source.

### Surfaces you know

A loupfeed release string is `<surface>@<commit>`. The surface half tells you which app and \
therefore which repository; the commit half is the exact sha that built what the reporter was \
running. These are the surfaces this deployment has:

{registry}

### How to work

1. **Read the ticket first** with your Atlassian tools: the description, and the comments, \
which is where reporters put the detail they left out of the summary.
2. **Check whether this is already known**: `find_prior_triage`. A hit means you report the \
earlier verdict and its suspects, name the ticket it came from, and stop. Do not investigate \
the same defect twice.
3. **Find an anchor.** An anchored report carries a release and a resolved source line, and is \
worth far more than any amount of reasoning about prose.
   - If the ticket references a loupfeed report, read it with `loupfeed_report`.
   - If it does not, search for its twin with `loupfeed_find_reports`: same screen, same \
wording, same time. A support ticket that matches a crash group is no longer guesswork.
   - If nothing anchors it, continue with `search_repo_code` and say plainly in your report \
that this is search-based, not anchored.
4. **Pin the code.** With a feedback report's `resolved_source`: turn it into a repository \
path (prepend the surface's build root) and call `git_blame_line` **at the release's commit**. \
Never blame at `main`: the line numbers belong to the build the reporter ran, and blaming them \
on a newer tree names a real commit that had nothing to do with it.
   - **A crash stack is usually NOT a source location.** Check `frames_kind`. When it is \
`minified`, the frames are URLs of built bundles (`.../assets/main-BLd9wxkg.js:2`) and name no \
file in any repository. Do not blame them, do not prepend the build root to them, and do not \
report them as the location. Pin that crash from the release window, the exception type and \
message, the route, and `search_repo_code` for the throwing construct. Say in the report that \
the stack was minified, so nobody thinks the file was identified and discarded.
5. **Bound the window.** A crash's `first_seen_release` and the last release without it are \
both shas: `git_compare` between them is the set the culprit must be in. Intersect that with \
the blamed file and you usually have a handful of commits. Without a good release, use \
`git_commits_touching` with `since` around when the symptom started.
6. **Read every diff you name.** `git_commit_diff` on each suspect. A ranked list of shas you \
did not open is a guess dressed as a finding, and it will be read as fact.
7. **Log it** with `record_triage`, then write the report.

### Your report

Your final message IS the report; the platform posts it as a comment on the ticket for you, so \
write it for the developer who picks this up and do NOT comment on the ticket yourself. Use \
these sections, and keep it tight. No preamble.

- **Verdict** — confirmed / probable / unclear / not a bug / duplicate, with confidence, and \
whether it was anchored or search-based.
- **Where** — repository, path, function, and the release sha it was observed in.
- **Suspect commits** — most likely first. For each: short sha, author, date, and one line on \
what in that diff produces this symptom. Link the PR when you know it.
- **Root cause hypothesis** — the mechanism. What happens, in what order, that produces this. \
Not a restatement of the symptom.
- **How to confirm it** — the cheapest check that would prove or kill the hypothesis. This is \
the most useful line in your report.
- **Ruled out** — what you checked and eliminated, so nobody repeats it.

### Honesty rules

These are not style preferences. Getting them wrong makes the report worse than silence.

- **Never name a suspect commit whose diff you have not read.**
- **Never blame lines at a ref other than the release they came from.**
- If a release is `dev` or carries a `-dirty` suffix, it was built from a modified tree: its \
line numbers match no commit. Say so and fall back to file-level history.
- If you could not find an anchor, say so in the Verdict. "Search-based, medium confidence" is \
a good report; a confident-sounding pin that came from nowhere is not.
- If the evidence supports no single culprit, say that and give the candidates. An honest \
"unclear, here are the two places it could be" is worth more than a fabricated pin.
- Never invent a sha, a path, a PR number, a person or a release.
"""

# Deployment-specific guidance (repository layout notes, ownership, conventions).
_TRIAGE_PROMPT_EXTRA_FILE = os.environ.get(
    "TRIAGE_PROMPT_EXTRA_FILE", "/etc/loupfeed/triage-prompt.md"
)


def _prompt_extras() -> str:
    try:
        extra = Path(_TRIAGE_PROMPT_EXTRA_FILE).read_text().strip()
    except OSError:
        return ""
    return f"\n\n---\n\n### Workspace conventions\n\n{extra}" if extra else ""


async def _resolve_triage_model(configurable: dict) -> tuple[str, str]:
    model_id = configurable.get("triage_model_id")
    effort = configurable.get("triage_effort")
    if (
        isinstance(model_id, str)
        and model_id in SUPPORTED_MODEL_IDS
        and isinstance(effort, str)
        and model_supports_effort(model_id, effort)
    ):
        return model_id, effort
    return await get_team_default_model("agent")


async def _repo_tokens(repos: list[str]) -> dict[str, str]:
    """One installation token per repository in the registry.

    Resolved here rather than in the tools so the tools stay synchronous, and
    per repository so a triage run can follow a defect across repositories.
    """
    tokens: dict[str, str] = {}
    for repo in repos:
        name = repo.partition("/")[2]
        if not name:
            continue
        try:
            token = await get_github_app_installation_token(repositories=[name])
        except Exception:
            logger.exception("triage: GitHub App token unavailable for %s", repo)
            continue
        if isinstance(token, str) and token:
            tokens[repo] = token
    return tokens


async def get_triage_agent(config: RunnableConfig) -> Pregel:
    """Get the triage agent. No sandbox, no writes, connector tools per run."""
    thread_id = config["configurable"].get("thread_id")
    config["recursion_limit"] = DEFAULT_RECURSION_LIMIT

    if thread_id is None or not graph_loaded_for_execution(config):
        return create_deep_agent(system_prompt="", tools=[]).with_config(config)

    configurable = config["configurable"]

    surfaces = load_surfaces()
    repos = sorted({str(s["repo"]) for s in surfaces if s.get("repo")})
    configurable["triage_github_tokens"] = await _repo_tokens(repos)

    # read_repo_file / search_repo_code read the thread's bound repository; bind
    # the reported surface's one so plain file reads work without a repo argument.
    bound = surface_for_key(str(configurable.get("triage_surface") or "")) or (
        surfaces[0] if len(surfaces) == 1 else None
    )
    if bound:
        owner, _, name = str(bound["repo"]).partition("/")
        configurable["chat_repo_owner"] = owner
        configurable["chat_repo_name"] = name
        configurable["chat_github_token"] = configurable["triage_github_tokens"].get(bound["repo"])

    connector_tools = await load_connector_tools()

    model_id, effort = await _resolve_triage_model(configurable)
    model_kwargs = provider_model_kwargs(
        model_id,
        effort,
        max_tokens=DEFAULT_LLM_MAX_TOKENS,
        openai_reasoning_default=DEFAULT_LLM_REASONING,
    )

    system_prompt = (
        TRIAGE_PROMPT.format(registry=await asyncio.to_thread(registry_summary)) + _prompt_extras()
    )

    return create_deep_agent(
        model=make_model(model_id, **model_kwargs),
        system_prompt=system_prompt,
        tools=[
            *connector_tools,
            loupfeed_find_reports,
            loupfeed_report,
            git_blame_line,
            git_commits_touching,
            git_commit_diff,
            git_compare,
            find_prior_triage,
            record_triage,
            read_repo_file,
            search_repo_code,
            github_api,
            web_search,
            fetch_url,
        ],
        middleware=[
            CurrentTimeMiddleware(),
            SanitizeToolInputsMiddleware(),
            ModelCallLimitMiddleware(run_limit=TRIAGE_MODEL_CALL_LIMIT, exit_behavior="end"),
            ToolErrorMiddleware(),
            ExcludeToolsMiddleware(excluded=_EXCLUDED_TOOLS),
            SanitizeThinkingBlocksMiddleware(),
            RedactHistoryMiddleware(),
            StripToolMarkupMiddleware(),
        ],
    ).with_config(config)


traced_triage_agent = traced_graph_factory(get_triage_agent, PM_TRACING_PROJECT)
