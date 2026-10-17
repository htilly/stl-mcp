# stl-mcp

Self-hosted MCP server where Claude creates and iteratively adjusts 3D models for printing (mm),
with a web interface for viewing, rotating and browsing the models.

- **MCP** (streamable HTTP) at `/mcp`
- **Library** at `/`: all models with preview, search, live updates
- **3D view** at `/m/<id>`: rotate/zoom, show/hide parts, version history, edit parameters,
  download STL. The view updates **immediately** when Claude changes the model.
- **Comments**: click a point on the model and write what should change. Claude sees the
  point and which part of the recipe is located there.
- **Templates**: screw holes (M2–M5, countersunk/cylindrical, nut trap), heat inserts, screw bosses,
  snap hooks, hinges and text.

## The idea: models are recipes, not files

Each model is stored as a *recipe* (JSON) with version history. The STL files are built from the recipe.
This lets Claude make small, precise changes (`{"wall": 3}`) instead of redoing everything,
and you can always go back to an earlier version.

Typical workflow, with the 3D view open in the browser next to the chat:

1. *"Make a pen holder with three holes for 12 mm pens"* → Claude calls `create_model` and
   gives you the link to the 3D view.
2. Claude calls `render_model` to see the result itself and fix obvious mistakes.
3. *"Four holes, and a bit taller"* → `update_model(id, {"n": 4, "h": 50})` → the view updates and
   the history shows v2.
4. You can also change the parameters directly in the web UI. Claude sees the change the next time it reads
   the model with `get_model`.
5. Or point: click **💬 Comment**, click the hole and write *"2 mm larger"*. Then say
   *"look at the comments"*. Claude gets the point, the part and `near_nodes`, that is, which node in
   the recipe is located there (e.g. `/root/children/1`, a `screw_hole`). It changes the model
   and closes the comment with `update_model(..., resolves=["c1"])`, or answers with
   `reply_to_comment`.

## Running
```
cp .env.example .env     # fill in MCP_TOKEN and PUBLIC_URL
docker compose up -d --build
```
Without Docker: `pip install -r requirements.txt && MCP_TOKEN=... python server.py`

The container only listens on `127.0.0.1:8765`. Set `BIND=0.0.0.0` to expose the port directly,
or set `DOMAIN` and run `docker compose --profile caddy up -d` for automatic HTTPS.

**Automatic deploy** to your own server with a self-hosted GitHub runner: see
[`deploy/README.md`](deploy/README.md).

Open `http://localhost:8765/` and log in with the token, or go to `/?token=<token>`, which
sets a cookie.

## Tests
```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```
`test_client.py` is a manual smoke script against a running server, not part of the suite.

## Connecting to Claude
- Claude Desktop/Code: `claude mcp add --transport http stl https://<your-url>/mcp --header "Authorization: Bearer <token>"`
- claude.ai: Settings → Connectors → Add custom connector → `https://<your-url>/mcp`
  (claude.ai cannot send a custom header. Put the server behind your reverse proxy/OAuth,
  or use Claude Desktop/Code.)

## MCP tools
| tool | |
|---|---|
| `create_model` | new model from a CSG spec (shapes, boolean operations, transforms, variables) |
| `create_enclosure` | box + lid with corner screws, standoffs, cutouts and lid holes |
| `create_from_script` | arbitrary trimesh Python (requires `ALLOW_SCRIPT=1`, runs code on the server!) |
| `update_model` | change variables/parameters or any value via JSON pointer → new version |
| `get_model` | the recipe, dimensions, history and open comments (with `near_nodes`) |
| `reply_to_comment` | reply to a comment, optionally marking it resolved |
| `render_model` | PNG image (iso/front/top/right) so Claude can inspect the model visually |
| `list_models`, `revert_model`, `duplicate_model`, `delete_model` | |
| `import_file` | imports STL/OBJ/GLB/PLY/OFF via `url` or `data_base64` (zip with obj+mtl is unpacked) |
| `file_info` | dimensions, bounds, volume, number of loose parts |
| `modify_file` | scale, rotate, mirror, place, add/subtract shapes (e.g. screw holes), repair mesh, simplify |
| `split_file` | splits a file into its loose parts |
| `export_obj` | merges several STLs (including library ones, `<model_id>/v3/lid.stl`) into one colored OBJ (+ .mtl and .zip) |
| `list_files`, `delete_file` | standalone files (import/export) |

### The CSG spec in brief
```json
{
  "vars": {"d": 12, "wall": 2, "h": 40, "n": 3},
  "root": {"op": "difference", "children": [
    {"shape": "box", "size": ["=n*(d+wall)+wall", "=d+2*wall", "=h"], "radius": 3, "round": "vertical"},
    {"shape": "cylinder", "d": "=d", "h": "=h", "translate": ["=wall+d/2", "=wall+d/2", "=wall"],
     "array": {"count": ["=n", 1, 1], "step": ["=d+wall", 0, 0]}}
  ]}
}
```
- Shapes: `box` (with `radius`), `cylinder`, `cone`, `sphere`, `tube`, `torus`, `wedge`,
  `extrude` (polygon/circle/rectangle profile, `twist`, `scale_top`) and `revolve`
- Templates: `screw_hole`, `heat_insert`, `nut_trap` (cutting: the surface is at z=0, going downward),
  `boss`, `snap_hook`, `hinge` (print-in-place or with a separate pin) and `text`
  (DejaVu Sans, raised or engraved). Dimensions for M2–M5 are in `templates.py`.
- Operations: `union`, `difference`, `intersection`, `hull`, `minkowski`
- All nodes: `translate`, `rotate` (degrees), `scale`, `mirror`, `array`, `polar`, `hidden`
- Multiple parts (one STL per part): `{"parts": {"body": {...}, "lid": {...}}}`
- Strings starting with `=` are expressions: `"=w/2+wall"`, `"=max(a,b)"`, `"=cos(30)"`
  (safe evaluation, no code is run)

The full reference is in `csg.py` (`SPEC_HELP`), and Claude gets it in the tool description.

### Adjusting with `update_model`
```jsonc
{"changes": {"wall": 3}}                          // CSG: variable; box: parameter
{"changes": {"/root/children/1/d": 8}}            // any value via JSON pointer
{"changes": {"/root/children/-": {"shape": ...}}} // add a node
{"remove": ["/root/children/2"]}                  // remove a node
{"params": {...}}                                 // replace the whole recipe
{"changes": {...}, "resolves": ["c1"]}            // ...and mark comment c1 as resolved
```

Example with templates, a plate with countersunk M3 holes and engraved text:
```json
{"vars": {"t": 4}, "root": {"op": "difference", "children": [
  {"shape": "box", "size": [60, 30, "=t"], "radius": 3, "round": "vertical"},
  {"shape": "screw_hole", "size": "M3", "head": "countersunk", "h": "=t",
   "translate": [8, 15, "=t"], "array": {"count": [2, 1, 1], "step": [44, 0, 0]}},
  {"shape": "text", "text": "OK", "size": 8, "h": 1, "halign": "center", "valign": "center",
   "translate": [30, 15, "=t-0.6"]}
]}}
```

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

Imported and modified files are standalone files in `STL_DIR` (`/files/<name>`). They do not show up
in the web library.

## Storage
`STL_DIR/models/<id>/model.json` (recipe and history) and `v<N>/<part>.stl`. The 50 most recent
versions keep their files. Older versions can be recreated with `revert_model`.

## Security
Always set `MCP_TOKEN` and put the server behind HTTPS. The web interface requires a token (cookie)
unless `UI_PUBLIC=1`, which makes it readable by everyone while changes still require a token.
`FILES_PUBLIC=1` only makes the STL links clickable without a token (model ids are guessable). Keep
`ALLOW_SCRIPT` off if the server is reachable from outside. `/upload/*` always requires a token when
`MCP_TOKEN` is set. The CSG spec is safe to let Claude write.

three.js (MIT) is shipped locally from `web/vendor/three`, so the interface needs no CDN.
The DejaVu Sans font (free license, see `fonts/LICENSE`) is used for the text template.
