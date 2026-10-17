# stl-mcp

Självhostad MCP-server som skapar STL-filer (mm) åt Claude. Streamable HTTP på `/mcp`,
nedladdning på `/files/<namn>.stl`.

## Köra
```
cp .env.example .env     # fyll i MCP_TOKEN och PUBLIC_URL
docker compose up -d --build
```
Utan Docker: `pip install -r requirements.txt && MCP_TOKEN=... python server.py`

## Koppla till Claude
- Claude Desktop/Code: `claude mcp add --transport http stl https://<din-url>/mcp --header "Authorization: Bearer <token>"`
- claude.ai: Settings → Connectors → Add custom connector → `https://<din-url>/mcp`
  (claude.ai kan inte skicka egen header; lägg då servern bakom din reverse proxy/OAuth, eller kör via Claude Desktop/Code.)

## Verktyg
- `create_enclosure` – låda + lock med hörnskruvar, distanser, utskärningar, lockhål
- `create_primitive` – box, cylinder, sphere, cone, tube
- `create_from_script` – godtycklig trimesh-Python (kräver `ALLOW_SCRIPT=1`, kör kod på servern!)
- `list_files`, `delete_file`

## Säkerhet
Sätt alltid `MCP_TOKEN` och lägg servern bakom HTTPS. `FILES_PUBLIC=1` gör nedladdningslänkarna
klickbara utan token (filnamnen är gissningsbara). Lämna `ALLOW_SCRIPT` av om servern är nåbar utifrån.
