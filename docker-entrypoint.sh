#!/bin/sh
# Runs the server as the unprivileged "app" user. Data volumes from before this image used a
# non-root user are owned by root, so hand them over once before dropping privileges.
set -e
if [ "$(id -u)" = "0" ]; then
  if [ -d "$STL_DIR" ] && [ "$(stat -c %u "$STL_DIR")" != "$(id -u app)" ]; then
    chown -R app:app "$STL_DIR"
  fi
  exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
