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
- `import_file` – tar in STL/OBJ/GLB/PLY/OFF via `url` eller `data_base64` (zip med obj+mtl packas upp)
- `file_info` – mått, bounds, volym, antal lösa delar
- `modify_file` – skala, rotera, spegla, placera, lägg till/skär bort former (t.ex. skruvhål), laga nät, förenkla
- `split_file` – delar upp en fil i sina lösa delar
- `export_obj` – slår ihop flera STL till en färgad OBJ (+ .mtl och .zip)
- `list_files`, `delete_file`

## Tinkercad
Tinkercad har inget offentligt API, så utbytet går via filer:

- **Till Tinkercad:** ladda ned `.stl` (en del) eller `export_obj`-resultatet (flera färgade delar) och
  välj *Import* i Tinkercad. Filer större än `TINKERCAD_MAX_MB` (default 25) får automatiskt en
  förenklad kopia `<namn>_tinkercad.stl`.
- **Från Tinkercad:** *Export* → `.STL` eller `.OBJ` (zip). Ladda upp filen till servern:
  ```
  curl -T modell.stl -H "Authorization: Bearer <token>" https://<din-url>/upload/modell.stl
  ```
  eller låt Claude hämta den med `import_file` (url). Bygg sedan vidare med `modify_file`.
  `import_file` hämtar bara från publika adresser om inte `ALLOW_PRIVATE_URLS=1`.

## Säkerhet
Sätt alltid `MCP_TOKEN` och lägg servern bakom HTTPS. `FILES_PUBLIC=1` gör nedladdningslänkarna
klickbara utan token (filnamnen är gissningsbara). `/upload/*` kräver alltid token när `MCP_TOKEN` är satt. Lämna `ALLOW_SCRIPT` av om servern är nåbar utifrån.
