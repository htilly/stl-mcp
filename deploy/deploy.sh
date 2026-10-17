#!/usr/bin/env bash
# Körs av GitHub Actions på servern (self-hosted runner) från repots checkout.
# Bygger och startar containern, väntar på /health och visar loggar vid fel.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/stl-mcp}       # beständig plats för data och .env
: "${MCP_TOKEN:?MCP_TOKEN saknas – lägg till som repo-secret}"

mkdir -p "$APP_DIR/data"
umask 077
cat > "$APP_DIR/.env" <<EOF
MCP_TOKEN=$MCP_TOKEN
PUBLIC_URL=${PUBLIC_URL:-http://localhost:8765}
DOMAIN=${DOMAIN:-}
UI_PUBLIC=${UI_PUBLIC:-0}
FILES_PUBLIC=${FILES_PUBLIC:-0}
ALLOW_SCRIPT=0
DATA_DIR=$APP_DIR/data
EOF

profiles=()
if [ -n "${DOMAIN:-}" ]; then
  profiles=(--profile caddy)           # inbyggd HTTPS via Caddy (kräver ledig port 80/443)
fi

compose=(docker compose --project-name stl-mcp --env-file "$APP_DIR/.env" "${profiles[@]}")
"${compose[@]}" up -d --build --remove-orphans

for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8765/health >/dev/null; then
    echo "stl-mcp är uppe (${PUBLIC_URL:-http://localhost:8765})"
    docker image prune -f >/dev/null
    exit 0
  fi
  sleep 1
done
echo "::error::stl-mcp svarade inte på /health"
"${compose[@]}" logs --tail 100
exit 1
