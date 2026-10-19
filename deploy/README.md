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
  (MCP's streamable HTTP transport uses SSE). It must pass every path through unchanged,
  including `/.well-known/oauth-*`, `/authorize`, `/token`, `/register` and `/revoke`, which
  claude.ai uses to connect. `PUBLIC_URL` must be `https://stl.tilly.se` exactly: it is the
  OAuth issuer and the passkeys' relying party id.
- Users, passkeys, sessions, OAuth tokens and the model metadata live in Postgres. See
  "Going live with users" below before deploying a version that has them.

The repo's own `docker-compose.yml` (with `build: .` and the optional Caddy profile) is for
running stl-mcp on other machines; tillyse does not read it.

## The Python sandbox (one time, for build123d)

build123d models, sandboxed script models and STEP import need the sandbox service. Until it is
set up they answer "needs the sandbox" and everything else works as before. On tillyse:

1. Add a service `stl-mcp-sandbox` to `/config/docker-compose.yml`, on the same image, copying
   the `sandbox` service from this repo's `docker-compose.yml`: `image: ghcr.io/htilly/stl-mcp:main`,
   `entrypoint: ["python", "sandboxd.py"]`, `network_mode: none`, `read_only: true`,
   `tmpfs: ["/tmp:size=1g,mode=1777"]`, `cap_drop: [ALL]`, `cap_add: [SETUID, SETGID, KILL]`,
   `security_opt: ["no-new-privileges:true"]`, `mem_limit: 4g`, `cpus: 2`, `pids_limit: 512`,
   `environment: {PYTHONDONTWRITEBYTECODE: "1"}` and a named volume on `/run/sandbox`. No env
   file and no other volumes: it must not see any secret. Optionally `runtime: runsc` if gVisor
   is installed.
2. Mount the same named volume on `/run/sandbox` in `stl-mcp` and add
   `SANDBOX_SOCKET=/run/sandbox/sandbox.sock` to `/config/secrets/stl-mcp.env`.
3. Start it once (`docker compose ... up -d stl-mcp-sandbox`; its log says `SANDBOX_READY`), then
   recreate `stl-mcp`. From then on every deploy recreates `stl-mcp-sandbox` before `stl-mcp`
   (deploy.yml does that when the service exists). A rollback only rolls back `stl-mcp`: if a
   change ever needs both at the same version, roll back `stl-mcp-sandbox` by hand too.

`ALLOW_SCRIPT` stays `0`: with the sandbox, script models no longer need it.

## Going live with users (one time)

From the "users" release on (PRs #11-#15), the server needs Postgres and refuses to start without
it, so the deploy rolls back until this is done. On tillyse, outside this repo:

1. **Postgres.** Add a `stl-mcp-db` service to `/config/docker-compose.yml` (`postgres:17-alpine`,
   on `config_default`, data in `/config/stl-mcp/pgdata`, `restart: unless-stopped`), and start it.
2. **Secrets** in `/config/secrets/stl-mcp.env`:
   - `DATABASE_URL=postgresql://stl:<password>@stl-mcp-db:5432/stl`
   - `ADMIN_EMAILS=<your email>`: the bootstrap admin, the one `MCP_TOKEN` logs in as.
   - `TRUSTED_PROXY=1`: SWAG sets `X-Forwarded-For`. Without it, the signup limit counts every
     visitor as SWAG's own address.
3. **Back up first.** Copy `/config/stl-mcp/data` before the first deploy. On first start the
   server moves `auth.json`, every `models/*/model.json` and the loose files in `data/` into the
   admin account. The originals are renamed `*.imported`, not deleted.
4. **Deploy** (merge to `main`). The startup log shows `database migrations applied: ...` and the
   import counts. Check that passkey login and the claude.ai connector still work.
5. **Backups.** Add a daily `pg_dump` (for example `docker exec stl-mcp-db pg_dump -U stl stl |
   gzip > /config/backups/stl-mcp-$(date +%F).sql.gz`) next to the copy of `data/`. A restore
   needs both.

Opening up to everyone is a separate step, once the above runs fine:

6. **Turnstile.** In Cloudflare create a Turnstile widget for `stl.tilly.se` and set
   `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET`.
7. **Sign in with providers** (optional; each one appears when its values are set). Redirect URI:
   `https://stl.tilly.se/auth/sso/<google|github|apple>/callback`. See the README section
   "Sign in with Google, GitHub or Apple".
8. **`SIGNUP=1`.** Optionally set `MAX_MODELS_PER_USER` and `MAX_DISK_MB_PER_USER`. Keep
   `ALLOW_SCRIPT=0` and `FILES_PUBLIC=0`. Watch new accounts under Admin → Users. Disable or
   delete them there, and set `SIGNUP=0` to close signup again.

## Rolling back manually

Each deploy tags the previous image `rollback-<timestamp>` (see the run summary). On tillyse:
```bash
docker tag ghcr.io/htilly/stl-mcp:rollback-<timestamp> ghcr.io/htilly/stl-mcp:main
cd /config && docker compose -p config --env-file /config/stl-mcp-runner/compose.env \
  -f /config/docker-compose.yml up -d --no-deps --force-recreate stl-mcp
```
