# Deploy to tilly.se (self-hosted GitHub runner)

The workflow `.github/workflows/deploy.yml` runs on push to `main` (or manually via *Run workflow*):

1. **test** (GitHub-hosted): installs dependencies, starts the server and runs `test_client.py`
   and auth checks.
2. **deploy** (on the server, runner label `tillyse`): `deploy/deploy.sh` writes
   `/opt/stl-mcp/.env`, runs `docker compose up -d --build` and waits for `/health`.
   Data lives in `/opt/stl-mcp/data` and survives redeploys.

## One-time setup

1. **Runner token:** GitHub → repo → Settings → Actions → Runners → *New self-hosted runner*
   → copy the string after `--token`. It is valid for one hour.
2. **On the server** (as root):
   ```
   curl -fsSL https://raw.githubusercontent.com/htilly/stl-mcp/main/deploy/setup-runner.sh -o setup-runner.sh
   sudo bash setup-runner.sh <TOKEN>
   ```
   Before the merge the script only exists on the PR branch. In that case replace `main` with
   `feature/web-ui-parametric-models` in the URL.
   The script installs Docker if missing, creates the user `gh-runner` and registers the
   runner as a systemd service.
3. **Repo settings** (Settings → Secrets and variables → Actions):
   - Secret `MCP_TOKEN`: a long random string, e.g. from `openssl rand -hex 32`
   - Variable `PUBLIC_URL`: e.g. `https://stl.tilly.se`
   - Variable `DOMAIN` (optional): e.g. `stl.tilly.se` starts the built-in Caddy with automatic
     HTTPS. This requires DNS pointing at the server and ports 80/443 being free.
   - Variables `UI_PUBLIC`, `FILES_PUBLIC` (optional): `0`/`1`
4. **Run** Actions → Deploy → *Run workflow*, or push to `main`.

## HTTPS when ports 80/443 are already in use (e.g. by the mail server)

Leave `DOMAIN` empty. The app then only listens on `127.0.0.1:8765`. Add a vhost to the
existing web server. Nginx:
```nginx
server {
    server_name stl.tilly.se;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_buffering off;          # live updates (SSE) and MCP streams
        proxy_read_timeout 1h;
    }
    # + certificate (certbot --nginx -d stl.tilly.se)
}
```

## Security
- The workflow is only triggered by push to `main` and manually, never by pull requests. Code from
  forks is therefore not run on the server. If the repo is public: still set Settings → Actions →
  *Require approval for all outside collaborators*.
- The runner user is in the `docker` group, which effectively gives root on the server. Only give
  trusted people write access to `main`.
