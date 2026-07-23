# Sandbox deployment (single-box, AWS)

Run the loupfeed agents platform on a single Linux box (tested on Amazon Linux
2023 / EC2) with `SANDBOX_TYPE=local`: the agent's tools execute directly on
the box instead of a hosted sandbox provider.

## Layout

- **Repo** at `/opt/loupfeed-agents`; systemd unit `loupfeed-agents` runs
  `uv run langgraph dev --host 0.0.0.0 --port 2024` (hot-reloads `.py` edits;
  `.env` changes need `systemctl restart loupfeed-agents`).
- **Secrets**: an AWS Secrets Manager secret (default `loupfeed-agents/agent`, override via `/etc/loupfeed/render-config`)
  is the source of truth; `bin/loupfeed-render-env` (ExecStartPre) renders
  `/opt/loupfeed-agents/.env` (0600). The instance role must be able to read it.
- **Ingress**: expose :2024 to GitHub webhooks however your infra prefers
  (e.g. CloudFront VPC origin, ALB, or a tunnel).
- **Local sandbox proxy shims** (`bin/`): upstream assumes a hosted proxy that
  injects credentials; local mode has none. `agent-gh-token` mints a GitHub App
  installation token (cached ~40 min), `agent-git-credential` is the git
  credential helper, and `gh` shadows `/usr/bin/gh` to swap the dummy GH_TOKEN
  for a real one.
- **PATH rule**: anything the agent must invoke from its shell has to be on the
  systemd PATH (`/home/ec2-user/.local/bin:/usr/local/bin:/usr/bin:/bin`) — the
  execute tool runs a non-login `/bin/sh`, so login-shell profile entries
  (PNPM_HOME etc.) are invisible. Hence the `/usr/local/bin` symlinks.

## Required configuration

### Secrets Manager secret (JSON keys)

| Key | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | any OpenAI-compatible LLM endpoint (BYO-LLM) |
| `ANTHROPIC_API_KEY` | optional, for Anthropic models |
| `LLM_MODEL_ID` | optional — overrides the platform default model (e.g. `openai:deepseek/deepseek-v4-pro`) |
| `GITHUB_APP_ID`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` | your GitHub App |
| `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_INSTALLATION_ID` | webhook + installation |
| `GITHUB_APP_PRIVATE_KEY_B64` | App private key, base64-encoded PEM |

`LANGSMITH_API_KEY_PROD=bot-token-only-mode` is rendered as a deliberate dummy:
it flips `is_bot_token_only_mode()` so runs authenticate with the GitHub App
installation token instead of per-user tokens.

### Org overlay (`/etc/loupfeed/`, installed from your private config repo)

Everything org-specific stays out of this repo. Keep a small **private config
repo** with:

| File | Consumed by | Contents |
|---|---|---|
| `env/agent.env` | appended verbatim to the rendered `.env` | `ALLOWED_GITHUB_ORGS=<your-org>` (trigger allowlist), optional `LLM_MODEL_ID=...`, any other env overrides |
| `env/render-config` | sourced by the env renderer | `SECRET_ID=...`, `REGION=...`, `REPO_DIR=...` overrides |
| `prompt/working-env.md` | appended to the agent's working-environment prompt section (`WORKING_ENV_EXTRA_FILE`, default `/etc/loupfeed/working-env.md`) | your cloud-access conventions, repo workflow pointers, runtime notes. Must not contain bare `{` / `}`. |

## Fresh box

```sh
git clone <this repo> /opt/loupfeed-agents
git clone <your private config repo> /opt/loupfeed-config   # optional but recommended
sudo PRIVATE_CONFIG_DIR=/opt/loupfeed-config REPO_DIR=/opt/loupfeed-agents \
  /opt/loupfeed-agents/deploy/sandbox/bootstrap.sh
sudo systemctl start loupfeed-agents
```

The bootstrap also pins runtimes the agent commonly needs on the systemd PATH
(Node 24, pnpm 10.12.1 — pnpm 11's fatal ignored-build-scripts breaks common
monorepos) and installs Playwright chromium system libs via dnf.

## Gotchas

- `langgraph dev` persists threads (SQLite); stale thread history poisons
  re-runs on the same issue — reset with `DELETE http://localhost:2024/threads/{id}`.
- GitHub `GET /user` 403 in logs is benign (bot tokens can't read a user
  profile; falls through to the team default).
