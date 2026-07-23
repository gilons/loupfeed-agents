<div align="center">
  <h1>loupfeed agents</h1>
  <h3>Open-source platform for your team's internal agents — coding and product management — reachable from the tools you already work in.</h3>
</div>

<div align="center">
  <a href="https://opensource.org/licenses/MIT" target="_blank"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  <a href="https://github.com/langchain-ai/langgraph" target="_blank"><img src="https://img.shields.io/badge/Built%20on-LangGraph-blue" alt="Built on LangGraph"></a>
  <a href="https://github.com/langchain-ai/deepagents" target="_blank"><img src="https://img.shields.io/badge/Built%20on-Deep%20Agents-blue" alt="Built on Deep Agents"></a>
</div>

<br>

**loupfeed agents** is a self-hostable agents platform: install it once, connect your tools, and invoke specialized agents from every entry point your team already uses —

- **loupfeedcode** — the coding agent. `@`-mention it on a GitHub issue or PR (or from Slack / Linear); it clones the repo into an isolated sandbox, implements, validates, and opens a draft PR. A separate **reviewer** graph reviews PRs with a diff-anchored findings model, and an **analyzer** graph learns your repo's review style over time.
- **loupfeedpm** *(in development)* — the product-management agent. Answers questions and acts on your planning system (Jira/Confluence via the Atlassian Rovo MCP connector; Notion, Linear, and others later), triages ideas out of meeting transcripts, and keeps planning threads moving — invoked from Microsoft Teams chats, channels, and call chats.

One platform underneath both: durable **thread-scoped sessions** (a conversation thread on the surface ⇄ an agent thread here), a single place to configure **tools and connectors**, **BYO-LLM**, and git as a first-class citizen.

## Architecture

Built on [LangGraph](https://langchain-ai.github.io/langgraph/) + [Deep Agents](https://github.com/langchain-ai/deepagents). Each graph is an agent; all graphs share the platform's sessions, connectors, and configuration:

| Graph | Purpose |
|---|---|
| `agent` | The coding agent — plans, edits, tests, and opens PRs from an isolated sandbox. |
| `reviewer` | Read-only PR reviews with a single evolving findings model. |
| `analyzer` | Learns per-repo review style from history and its own finding outcomes. |
| `pm` *(planned)* | The PM agent — planning-system connectors (MCP), meeting-transcript triage, thread-native conversations. |

Key properties:

- **Isolated execution.** Every task runs in its own sandbox — Modal, Daytona, Runloop, LangSmith, or `local` mode on your own box — with full permissions *inside* the boundary and nothing outside it. Each thread's sandbox persists across follow-up messages, auto-recreates when unreachable, and tasks run in parallel without queuing.
- **Deterministic thread routing.** The same issue / PR / conversation always routes back to the same agent thread. Message the agent while it's working — mid-run messages are injected before its next step.
- **Curated tools, not accumulated ones.** A small toolset (`execute`, `fetch_url`, `http_request`, source-channel replies) plus the built-in Deep Agents file/search/subagent tools. Optional server-side observability tools (Datadog MCP, LangSmith) load only for authorized users' runs, with credentials that never enter the sandbox.
- **BYO-LLM.** Model selection is configuration, not code: any OpenAI-compatible endpoint (`OPENAI_BASE_URL`), Anthropic, per-user and team defaults, and model fallbacks.
- **Context from where the work lives.** A repo-root `AGENTS.md` is injected into the system prompt; the full issue / thread history rides along on invocation.
- **Org config stays out of the platform.** Deployments customize via an overlay — an env file plus an optional prompt-extension markdown appended to the agent's working-environment guidance — never by forking. See [deploy/sandbox/README.md](deploy/sandbox/README.md) for the contract.
- **Web dashboard** (`ui/`) — GitHub login, per-user model/profile settings, team defaults, enabled repos, review-style management, and an agents chat UI.

## Getting started

- **[INSTALLATION.md](INSTALLATION.md)** — local dev (backend + dashboard), GitHub App creation, triggers, production deployment.
- **[deploy/sandbox/README.md](deploy/sandbox/README.md)** — single-box AWS deployment (`SANDBOX_TYPE=local`): systemd unit, Secrets Manager-rendered env, GitHub App auth shims, and the org-overlay contract.
- **[CUSTOMIZATION.md](CUSTOMIZATION.md)** — swap the sandbox, model, tools, triggers, system prompt, and middleware.

```bash
make install   # uv pip install -e .
make dev       # langgraph dev — serves all graphs + the FastAPI webhook app
make test      # pytest
```

## Roadmap

The platform direction (multi-surface entry points, thread⇄session model, connector registry) is documented in `docs/11-agents-platform.md` of the [loupfeed](https://github.com/gilons/loupfeed) repo:

1. **Microsoft Teams entry point** — 1:1 chats, channel threads, and meeting chats; connector sign-in initiated from Teams.
2. **`pm` graph** — Atlassian Rovo MCP + git; generic connector registry (Notion, Linear, …).
3. **Meeting intelligence** — transcript-driven triage and classification after calls.
4. **Unified install** — one onboarding across GitHub + Teams with shared identity.

## License & credits

MIT. Derived from [Open SWE](https://github.com/langchain-ai/open-swe) by LangChain — thanks to its authors for the foundation this platform builds on.
