# stl-mcp

Self-hosted MCP server that creates STL files (mm) for Claude. Streamable HTTP on `/mcp`,
downloads at `/files/<name>.stl`.

## Running
```
cp .env.example .env     # fill in MCP_TOKEN and PUBLIC_URL
docker compose up -d --build
```
Without Docker: `pip install -r requirements.txt && MCP_TOKEN=... python server.py`

## Connecting to Claude
- Claude Desktop/Code: `claude mcp add --transport http stl https://<your-url>/mcp --header "Authorization: Bearer <token>"`
- claude.ai: Settings → Connectors → Add custom connector → `https://<your-url>/mcp`
  (claude.ai cannot send a custom header; put the server behind your reverse proxy/OAuth, or use Claude Desktop/Code.)

## Tests
```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

## Tools
- `create_enclosure` – box + lid with corner screws, standoffs, cutouts, lid holes
- `create_primitive` – box, cylinder, sphere, cone, tube
- `create_from_script` – arbitrary trimesh Python (requires `ALLOW_SCRIPT=1`, runs code on the server!)
- `import_file` – imports STL/OBJ/GLB/PLY/OFF via `url` or `data_base64` (zip with obj+mtl is unpacked)
- `file_info` – dimensions, bounds, volume, number of loose parts
- `modify_file` – scale, rotate, mirror, place, add/subtract shapes (e.g. screw holes), repair mesh, simplify
- `split_file` – splits a file into its loose parts
- `export_obj` – merges several STLs into one colored OBJ (+ .mtl and .zip)
- `list_files`, `delete_file`

## Tinkercad
Tinkercad has no public API, so exchange happens via files:

- **To Tinkercad:** download the `.stl` (one part) or the `export_obj` result (several colored parts) and
  choose *Import* in Tinkercad. Files larger than `TINKERCAD_MAX_MB` (default 25) automatically get a
  simplified copy `<name>_tinkercad.stl`.
- **From Tinkercad:** *Export* → `.STL` or `.OBJ` (zip). Upload the file to the server:
  ```
  curl -T model.stl -H "Authorization: Bearer <token>" https://<your-url>/upload/model.stl
  ```
  or let Claude fetch it with `import_file` (url). Then keep building with `modify_file`.
  `import_file` only fetches from public addresses unless `ALLOW_PRIVATE_URLS=1`.

## Security
Always set `MCP_TOKEN` and put the server behind HTTPS. `FILES_PUBLIC=1` makes the download links
clickable without a token (filenames are guessable). `/upload/*` always requires a token when `MCP_TOKEN` is set. Keep `ALLOW_SCRIPT` off if the server is reachable from outside.
