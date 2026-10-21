#!/bin/bash
# Start every new session on its own branch, cut from the latest origin/main.
# Runs on a fresh start only (not resume/clear/compact) and never touches
# uncommitted work or a branch that already has unmerged commits.
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}"

input="$(cat || true)"
read -r source session_id < <(printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    d = {}
print(d.get("source", "startup"), d.get("session_id", ""))
') || true

[ "${source:-startup}" = "startup" ] || exit 0

git fetch -q origin main || { echo "session-start: fetch of origin/main failed, branch left as is"; exit 0; }

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "session-start: uncommitted changes, branch left as is"
  exit 0
fi

if [ -n "$(git rev-list origin/main..HEAD 2>/dev/null | head -1)" ]; then
  echo "session-start: $(git rev-parse --abbrev-ref HEAD) has unmerged commits, branch left as is"
  exit 0
fi

branch="claude/session-${session_id:0:8}"
[ "$branch" != "claude/session-" ] || branch="claude/session-$(date +%Y%m%d-%H%M%S)"

git checkout -q -B "$branch" origin/main
echo "session-start: on new branch $branch from origin/main ($(git rev-parse --short HEAD))"
