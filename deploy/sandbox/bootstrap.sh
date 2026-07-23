#!/bin/bash
# Bootstrap the loupfeed agents box (Amazon Linux 2023) — reproduces the live
# dino-ai sandbox setup that previously existed only as manual patches.
#
# Idempotent. Run as root on the target instance after the repo is cloned to
# /opt/open-swe. Requires: the instance role can read the Secrets Manager
# secret named in bin/open-swe-render-env (default: agent-sandbox/open-swe).
set -euo pipefail

REPO_DIR=${REPO_DIR:-/opt/open-swe}
NODE_VERSION=${NODE_VERSION:-v24.5.0}
PNPM_VERSION=${PNPM_VERSION:-10.12.1}
HERE=$(cd "$(dirname "$0")" && pwd)

echo "== 1/6 sandbox proxy shims + env renderer =="
install -m 755 "$HERE/bin/open-swe-render-env" /usr/local/bin/open-swe-render-env
install -m 755 "$HERE/bin/agent-gh-token"      /usr/local/bin/agent-gh-token
install -m 755 "$HERE/bin/agent-git-credential" /usr/local/bin/agent-git-credential
# gh wrapper must shadow /usr/bin/gh via PATH order (systemd PATH puts /usr/local/bin first)
install -m 755 "$HERE/bin/gh"                  /usr/local/bin/gh

echo "== 2/6 git credential helper for ec2-user =="
runuser -u ec2-user -- git config --global credential.https://github.com.helper /usr/local/bin/agent-git-credential
runuser -u ec2-user -- git config --global --add safe.directory "$REPO_DIR"

echo "== 3/6 Node ${NODE_VERSION} (deliveru requires engines node 24.x) =="
if [ ! -d "/usr/local/lib/nodejs/node-${NODE_VERSION}-linux-x64" ]; then
  mkdir -p /usr/local/lib/nodejs
  curl -fsSL "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" \
    | tar -xJ -C /usr/local/lib/nodejs
fi
for b in node npm npx corepack; do
  ln -sf "/usr/local/lib/nodejs/node-${NODE_VERSION}-linux-x64/bin/$b" "/usr/local/bin/$b"
done

echo "== 4/6 pnpm ${PNPM_VERSION} (v11 breaks deliveru: ignored-build-scripts fatal) =="
# Installed under ec2-user's PNPM_HOME, symlinked to /usr/local/bin because the
# agent's execute tool runs a non-login /bin/sh where PNPM_HOME is not on PATH.
runuser -u ec2-user -- bash -c "curl -fsSL https://get.pnpm.io/install.sh | env PNPM_VERSION=${PNPM_VERSION} SHELL=/bin/bash sh -" || true
ln -sf /home/ec2-user/.local/share/pnpm/pnpm /usr/local/bin/pnpm 2>/dev/null \
  || ln -sf /home/ec2-user/.local/share/pnpm/bin/pnpm /usr/local/bin/pnpm

echo "== 5/6 Playwright chromium system libs (dnf — NOT playwright install-deps, which is apt-only) =="
dnf install -y -q nss nspr atk at-spi2-atk cups-libs libdrm libxkbcommon \
  libX11 libXcomposite libXdamage libXext libXfixes libXrandr \
  mesa-libgbm alsa-lib pango cairo || true

echo "== 6/6 workspace dir + systemd unit =="
mkdir -p /srv/agent-workspace && chown ec2-user:ec2-user /srv/agent-workspace
install -m 644 "$HERE/open-swe.service" /etc/systemd/system/open-swe.service
systemctl daemon-reload
systemctl enable open-swe

echo "bootstrap complete — start with: systemctl start open-swe"
