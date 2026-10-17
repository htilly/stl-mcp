#!/usr/bin/env bash
# Engångsinstallation på servern (Debian/Ubuntu), körs som root:
#
#   curl -fsSL https://raw.githubusercontent.com/htilly/stl-mcp/main/deploy/setup-runner.sh -o setup-runner.sh
#   sudo bash setup-runner.sh <REGISTRERINGSTOKEN>
#
# Token hämtas på GitHub: repo → Settings → Actions → Runners → New self-hosted runner
# (strängen efter --token, giltig i en timme).
#
# Installerar Docker (om det saknas), skapar användaren gh-runner (medlem i docker-gruppen)
# och registrerar en GitHub Actions-runner med label "tillyse" som systemd-tjänst.
set -euo pipefail

TOKEN=${1:?användning: setup-runner.sh <registreringstoken> [repo-url]}
REPO_URL=${2:-https://github.com/htilly/stl-mcp}
RUNNER_USER=gh-runner
RUNNER_DIR=/opt/actions-runner
APP_DIR=/opt/stl-mcp
LABELS=tillyse

[ "$(id -u)" = 0 ] || { echo "kör som root (sudo)"; exit 1; }

apt-get update -qq
apt-get install -y -qq curl tar jq ca-certificates >/dev/null

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "Installerar Docker…"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

id "$RUNNER_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$RUNNER_USER"
usermod -aG docker "$RUNNER_USER"
mkdir -p "$RUNNER_DIR" "$APP_DIR/data"
chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR" "$APP_DIR"

case "$(uname -m)" in
  x86_64) ARCH=x64 ;;
  aarch64) ARCH=arm64 ;;
  *) echo "okänd arkitektur $(uname -m)"; exit 1 ;;
esac
VERSION=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')
if [ ! -x "$RUNNER_DIR/config.sh" ]; then
  echo "Hämtar actions-runner $VERSION ($ARCH)…"
  curl -fsSL "https://github.com/actions/runner/releases/download/v$VERSION/actions-runner-linux-$ARCH-$VERSION.tar.gz" \
    | sudo -u "$RUNNER_USER" tar xz -C "$RUNNER_DIR"
  "$RUNNER_DIR/bin/installdependencies.sh" >/dev/null
fi

cd "$RUNNER_DIR"
sudo -u "$RUNNER_USER" ./config.sh --unattended --replace \
  --url "$REPO_URL" --token "$TOKEN" \
  --name "$(hostname -s)-stl" --labels "$LABELS" --work _work

./svc.sh install "$RUNNER_USER"
./svc.sh start

echo
echo "Klart. Runnern '$(hostname -s)-stl' (label: $LABELS) är registrerad och körs som tjänst."
echo "Nästa steg: lägg in secret MCP_TOKEN och variabeln PUBLIC_URL i repot, kör sedan Deploy-flödet."
