#!/bin/bash
# Bootstrap the loupfeed agents box (Amazon Linux 2023) — reproduces the live
# dino-ai sandbox setup that previously existed only as manual patches.
#
# Idempotent. Run as root on the target instance after the repo is cloned to
# /opt/open-swe. Requires: the instance role can read the Secrets Manager
# secret named in /etc/loupfeed/render-config (default: loupfeed-agents/agent).
set -euo pipefail

REPO_DIR=${REPO_DIR:-/opt/loupfeed-agents}
NODE_VERSION=${NODE_VERSION:-v24.5.0}
PNPM_VERSION=${PNPM_VERSION:-10.12.1}
HERE=$(cd "$(dirname "$0")" && pwd)

echo "== 1/7 sandbox proxy shims + env renderer =="
install -m 755 "$HERE/bin/loupfeed-render-env" /usr/local/bin/loupfeed-render-env
install -m 755 "$HERE/bin/agent-gh-token"      /usr/local/bin/agent-gh-token
install -m 755 "$HERE/bin/agent-git-credential" /usr/local/bin/agent-git-credential
# gh wrapper must shadow /usr/bin/gh via PATH order (systemd PATH puts /usr/local/bin first)
install -m 755 "$HERE/bin/gh"                  /usr/local/bin/gh

echo "== 2/7 git credential helper for ec2-user =="
runuser -u ec2-user -- git config --global credential.https://github.com.helper /usr/local/bin/agent-git-credential
runuser -u ec2-user -- git config --global --add safe.directory "$REPO_DIR"

echo "== 3/7 Node ${NODE_VERSION} (deliveru requires engines node 24.x) =="
if [ ! -d "/usr/local/lib/nodejs/node-${NODE_VERSION}-linux-x64" ]; then
  mkdir -p /usr/local/lib/nodejs
  curl -fsSL "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" \
    | tar -xJ -C /usr/local/lib/nodejs
fi
for b in node npm npx corepack; do
  ln -sf "/usr/local/lib/nodejs/node-${NODE_VERSION}-linux-x64/bin/$b" "/usr/local/bin/$b"
done

echo "== 4/7 pnpm ${PNPM_VERSION} (v11 breaks deliveru: ignored-build-scripts fatal) =="
# Installed under ec2-user's PNPM_HOME, symlinked to /usr/local/bin because the
# agent's execute tool runs a non-login /bin/sh where PNPM_HOME is not on PATH.
runuser -u ec2-user -- bash -c "curl -fsSL https://get.pnpm.io/install.sh | env PNPM_VERSION=${PNPM_VERSION} SHELL=/bin/bash sh -" || true
ln -sf /home/ec2-user/.local/share/pnpm/pnpm /usr/local/bin/pnpm 2>/dev/null \
  || ln -sf /home/ec2-user/.local/share/pnpm/bin/pnpm /usr/local/bin/pnpm

echo "== 5/7 Playwright chromium system libs (dnf — NOT playwright install-deps, which is apt-only) =="
dnf install -y -q nss nspr atk at-spi2-atk cups-libs libdrm libxkbcommon \
  libX11 libXcomposite libXdamage libXext libXfixes libXrandr \
  mesa-libgbm alsa-lib pango cairo || true

echo "== 6/7 org overlay (optional) =="
# Org-specific config lives OUTSIDE the platform repo (e.g. a private config
# repo). Point PRIVATE_CONFIG_DIR at a checkout containing:
#   env/agent.env        → appended to the rendered .env (ALLOWED_GITHUB_ORGS, LLM_MODEL_ID, ...)
#   env/render-config    → sourced by loupfeed-render-env (SECRET_ID, REGION, REPO_DIR)
#   prompt/working-env.md → appended to the agent's working-env prompt section
mkdir -p /etc/loupfeed
if [ -n "${PRIVATE_CONFIG_DIR:-}" ] && [ -d "$PRIVATE_CONFIG_DIR" ]; then
  [ -f "$PRIVATE_CONFIG_DIR/env/agent.env" ] && install -o ec2-user -g ec2-user -m 600 "$PRIVATE_CONFIG_DIR/env/agent.env" /etc/loupfeed/agent.env
  [ -f "$PRIVATE_CONFIG_DIR/env/render-config" ] && install -o ec2-user -g ec2-user -m 600 "$PRIVATE_CONFIG_DIR/env/render-config" /etc/loupfeed/render-config
  [ -f "$PRIVATE_CONFIG_DIR/prompt/working-env.md" ] && install -m 644 "$PRIVATE_CONFIG_DIR/prompt/working-env.md" /etc/loupfeed/working-env.md
  echo "installed org overlay from $PRIVATE_CONFIG_DIR"
else
  echo "no PRIVATE_CONFIG_DIR — platform runs with defaults (no org allowlist, upstream default model)"
fi

echo "== 7/7 workspace dir + systemd unit =="
mkdir -p /srv/agent-workspace && chown ec2-user:ec2-user /srv/agent-workspace
install -m 644 "$HERE/loupfeed-agents.service" /etc/systemd/system/loupfeed-agents.service
systemctl daemon-reload
systemctl enable loupfeed-agents

echo "bootstrap complete — start with: systemctl start loupfeed-agents"
