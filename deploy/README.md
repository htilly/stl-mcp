# Deploy to tillyse (stl.tilly.se)

Merging to `main` deploys automatically. Two workflows, mirroring the fiskeapp setup:

1. **`Build and Push Docker Image`** (`.github/workflows/docker-image.yml`, GitHub-hosted):
   runs the test suite, then builds the image and pushes `ghcr.io/htilly/stl-mcp:main` and
   `:main-<short sha>`, with the commit baked in as `GIT_SHA`.
2. **`Deploy to tillyse`** (`.github/workflows/deploy.yml`): triggered by a successful build.
   - A gate job on GitHub's runner requires a push to `main` in this repo (no forks, no PRs)
     and that both the build's actor and the re-run's actor are in `ALLOWED_ACTORS`.
   - The deploy job runs on the self-hosted runner `stl-mcp-runner` (labels `tillyse,stl-mcp`)
     in the `tillyse` environment, which only admits `main`.
   - It pulls `:main-<sha>`, recreates **only** the `stl-mcp` service, and fails unless the
     container logs `STL_MCP_READY sha=<that commit>` (printed by `server.py` once uvicorn is
     listening), runs exactly the pulled image, answers `/health`, returns 401 on `/mcp`
     without a token and accepts the token.
   - On failure it rolls back to the previous `:main` and notifies ntfy. The Actions log is
     public, so the startup log is only summarised in redacted form.

The runner talks to Docker through a socket proxy with `BUILD=0` and `EXEC=0`: nothing is ever
built on the server. Don't build `:main` locally on tillyse either - it would become the rollback
target and break the image-id provenance check.

## Server side (already set up, lives outside this repo)

- `/config/docker-compose.yml`: services `stl-mcp` (`127.0.0.1:8892` -> `8765`, data in
  `/config/stl-mcp/data`) and `stl-mcp-runner`.
- `/config/secrets/stl-mcp.env`: `MCP_TOKEN`, `PUBLIC_URL`, `FILES_PUBLIC=0`, `ALLOW_SCRIPT=0`.
- `/config/secrets/stl-mcp-runner.env`: `RUNNER_TOKEN`, `NTFY_TOKEN`, `MCP_TOKEN`.
- SWAG site-conf `stl.tilly.se.conf` proxies to `127.0.0.1:8892` with `proxy_buffering off`
  (MCP's streamable HTTP transport uses SSE).

The repo's own `docker-compose.yml` (with `build: .` and the optional Caddy profile) is for
running stl-mcp on other machines; tillyse does not read it.

## Rolling back manually

Each deploy tags the previous image `rollback-<timestamp>` (see the run summary). On tillyse:
```bash
docker tag ghcr.io/htilly/stl-mcp:rollback-<timestamp> ghcr.io/htilly/stl-mcp:main
cd /config && docker compose -p config --env-file /config/stl-mcp-runner/compose.env \
  -f /config/docker-compose.yml up -d --no-deps --force-recreate stl-mcp
```
