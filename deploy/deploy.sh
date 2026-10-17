#!/usr/bin/env bash
# Run by GitHub Actions on the server (self-hosted runner) from the repo checkout.
# Builds and starts the container, waits for /health and shows logs on failure.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/stl-mcp}       # persistent location for data and .env
: "${MCP_TOKEN:?MCP_TOKEN is missing – add it as a repo secret}"

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
  profiles=(--profile caddy)           # built-in HTTPS via Caddy (requires free port 80/443)
fi

compose=(docker compose --project-name stl-mcp --env-file "$APP_DIR/.env" "${profiles[@]}")
"${compose[@]}" up -d --build --remove-orphans

for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8765/health >/dev/null; then
    echo "stl-mcp is up (${PUBLIC_URL:-http://localhost:8765})"
    docker image prune -f >/dev/null
    exit 0
  fi
  sleep 1
done
echo "::error::stl-mcp did not respond on /health"
"${compose[@]}" logs --tail 100
exit 1
