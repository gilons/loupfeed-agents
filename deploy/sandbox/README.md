# Sandbox deployment (dino-ai box)

This directory reproduces, as code, the live setup of the loupfeed agents box
(EC2 `i-0dca1ca63c39114ca`, AWS acct 047194654444 `dino-ai`, eu-central-1) that
previously existed only as manual patches. See also
`loupfeed/docs/11-agents-platform.md` (platform architecture, M0).

## What runs where

- **Repo** at `/opt/open-swe`; systemd unit `open-swe` runs
  `uv run langgraph dev --host 0.0.0.0 --port 2024` (hot-reloads `.py` edits;
  `.env` changes need `systemctl restart open-swe`).
- **Secrets**: Secrets Manager `agent-sandbox/open-swe` is the source of truth;
  `bin/open-swe-render-env` (ExecStartPre) renders `/opt/open-swe/.env` (0600).
  Keys: LLM (OPENAI_API_KEY/OPENAI_BASE_URL for TensorIX, ANTHROPIC_API_KEY),
  GitHub App (id 4086794, installation 141136243), webhook secret.
  `LANGSMITH_API_KEY_PROD=bot-token-only-mode` is a deliberate dummy: it flips
  `is_bot_token_only_mode()` so runs use the GitHub App installation token.
- **Ingress**: GitHub webhook via CloudFront VPC origin → :2024.
- **Local sandbox proxy shims** (`bin/`): Open SWE upstream assumes a hosted
  proxy that injects credentials; local mode has none. `agent-gh-token` mints a
  GitHub App installation token (cached ~40 min), `agent-git-credential` is the
  git credential helper, `gh` shadows `/usr/bin/gh` and swaps the dummy
  GH_TOKEN for a real one.
- **PATH rule**: anything the agent must invoke from its shell has to be on the
  systemd PATH (`/home/ec2-user/.local/bin:/usr/local/bin:/usr/bin:/bin`) — the
  execute tool runs a non-login `/bin/sh`, so login-shell profile entries
  (PNPM_HOME etc.) are invisible. Hence the `/usr/local/bin` symlinks for node,
  pnpm, corepack.
- **Runtime pins**: Node 24 (deliveru engines), pnpm 10.12.1 (pnpm 11 makes
  ignored-build-scripts fatal and ignores `pnpm.onlyBuiltDependencies`).
- **deliveru specifics** (documented, not installed here): the box `~/.aws/config`
  `[profile deliveru-dev]` carries `role_session_name=openswe` so deliveru's
  `detect-stage.sh` resolves STAGE=openswe; react builds need
  `NODE_OPTIONS=--max-old-space-size=6144` on a t3a.large; loupfeed manifest
  upload is skipped at build with `LOUPFEED_BUILD_TOKEN=""`.

## Fresh box

```sh
git clone <this repo> /opt/open-swe
sudo REPO_DIR=/opt/open-swe /opt/open-swe/deploy/sandbox/bootstrap.sh
sudo systemctl start open-swe
```

## Gotchas

- `langgraph dev` persists threads (SQLite); stale thread history poisons
  re-runs on the same issue — reset with `DELETE http://localhost:2024/threads/{id}`.
- GitHub `GET /user` 403 in logs is benign (bot tokens can't read a user
  profile; falls through to team default).
- `ALLOWED_GITHUB_ORGS=dinolabdev` is currently set in the render script —
  parameterize when this becomes multi-tenant.
