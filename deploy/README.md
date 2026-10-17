# Deploy till tilly.se (self-hosted GitHub-runner)

Flödet `.github/workflows/deploy.yml` körs vid push till `main` (eller manuellt via *Run workflow*):

1. **test** (GitHub-hostad): installerar beroenden, startar servern och kör `test_client.py`
   och auth-kontroller.
2. **deploy** (på servern, runner-label `tillyse`): `deploy/deploy.sh` skriver
   `/opt/stl-mcp/.env`, kör `docker compose up -d --build` och väntar på `/health`.
   Data ligger i `/opt/stl-mcp/data` och överlever omdeployer.

## Engångsinstallation

1. **Runner-token:** GitHub → repo → Settings → Actions → Runners → *New self-hosted runner*
   → kopiera strängen efter `--token`. Den gäller i en timme.
2. **På servern** (som root):
   ```
   curl -fsSL https://raw.githubusercontent.com/htilly/stl-mcp/main/deploy/setup-runner.sh -o setup-runner.sh
   sudo bash setup-runner.sh <TOKEN>
   ```
   Före merge finns skriptet bara på PR-grenen. Byt då `main` mot
   `feature/web-ui-parametric-models` i URL:en.
   Skriptet installerar Docker om det saknas, skapar användaren `gh-runner` och registrerar
   runnern som en systemd-tjänst.
3. **Repo-inställningar** (Settings → Secrets and variables → Actions):
   - Secret `MCP_TOKEN`: en lång slumpsträng, t.ex. från `openssl rand -hex 32`
   - Variable `PUBLIC_URL`: t.ex. `https://stl.tilly.se`
   - Variable `DOMAIN` (valfri): t.ex. `stl.tilly.se` startar inbyggd Caddy med automatiskt
     HTTPS. Det kräver att DNS pekar på servern och att portarna 80/443 är lediga.
   - Variables `UI_PUBLIC`, `FILES_PUBLIC` (valfria): `0`/`1`
4. **Kör** Actions → Deploy → *Run workflow*, eller pusha till `main`.

## HTTPS när port 80/443 redan används (t.ex. av mailservern)

Låt `DOMAIN` vara tom. Appen lyssnar då bara på `127.0.0.1:8765`. Lägg till en vhost i den
befintliga webbservern. Nginx:
```nginx
server {
    server_name stl.tilly.se;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_buffering off;          # live-uppdateringar (SSE) och MCP-strömmar
        proxy_read_timeout 1h;
    }
    # + certifikat (certbot --nginx -d stl.tilly.se)
}
```

## Säkerhet
- Flödet triggas bara av push till `main` och manuellt, aldrig av pull requests. Kod från
  forkar körs alltså inte på servern. Om repot är publikt: ställ ändå in Settings → Actions →
  *Require approval for all outside collaborators*.
- Runner-användaren är med i `docker`-gruppen, vilket i praktiken ger root på servern. Ge
  bara betrodda personer skrivrätt till `main`.
