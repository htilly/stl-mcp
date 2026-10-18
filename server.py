"""STL-MCP: MCP server (streamable HTTP) + web UI for parametric 3D models.

Models are stored as recipes (kind + params) with version history. Claude creates and adjusts
models via MCP; the web UI shows the library and a 3D view that updates live.

Environment variables:
  STL_DIR        data directory                   (default ./out)
  PUBLIC_URL     base URL used in links           (default http://localhost:8765)
  MCP_TOKEN      bearer token; required if set (strongly recommended). Also signs web UI sessions
  OAUTH          0 = disable the OAuth server for MCP clients like claude.ai (default on;
                 needs PUBLIC_URL on https or localhost)
  UI_PUBLIC      1 = web UI + files readable without a token (changes require a token)
  FILES_PUBLIC   1 = only /files/* without a token (download links are clickable)
  ALLOW_SCRIPT   1 = enable script models (runs arbitrary Python!)
  TINKERCAD_MAX_MB  size limit for Tinkercad import (default 25); larger files get a simplified copy
  MAX_UPLOAD_MB  max size for import/upload (default 100)
  ALLOW_PRIVATE_URLS 1 = import_file may fetch from private/local addresses
  HOST / PORT    default 0.0.0.0 / 8765

Standalone files (Tinkercad import/export, modify_file etc.) live directly in STL_DIR and are
served at /files/<name>; the model library lives in STL_DIR/models and is served at /files/<id>/<version>/<part>.
"""
import asyncio
import base64
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import zipfile
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import anyio
import httpx
import uvicorn
import webauthn
from mcp.server.auth.provider import AuthorizeError
from mcp.server.auth.routes import (build_resource_metadata_url, create_auth_routes,
                                    create_protected_resource_routes)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                                 RedirectResponse, Response, StreamingResponse)
from starlette.routing import Mount, Route
from pydantic import AnyHttpUrl
from starlette.staticfiles import StaticFiles


class RevalidatedStatic(StaticFiles):
    """Static assets are not fingerprinted, so make every cache (Cloudflare included) revalidate
    against the ETag instead of guessing a lifetime. Otherwise a deploy keeps serving old CSS."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

import csg
import geometry as g
import printcheck
import render
from auth import (CHALLENGE_TTL, SCOPE, SESSION_TTL, AuthState, Expiring, OAuthProvider,
                  check_session, make_session)
from store import Store

DATA = Path(os.environ.get("STL_DIR", "./out")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8765").rstrip("/")
TOKEN = os.environ.get("MCP_TOKEN", "")
ALLOW_SCRIPT = os.environ.get("ALLOW_SCRIPT") == "1"
UI_PUBLIC = os.environ.get("UI_PUBLIC") == "1"
FILES_PUBLIC = UI_PUBLIC or os.environ.get("FILES_PUBLIC") == "1"
WEB = Path(__file__).parent / "web"
OUT = DATA  # standalone files
TINKERCAD_MAX = int(float(os.environ.get("TINKERCAD_MAX_MB", "25")) * 1_000_000)
MAX_UPLOAD = int(float(os.environ.get("MAX_UPLOAD_MB", "100")) * 1_000_000)
ALLOW_PRIVATE_URLS = os.environ.get("ALLOW_PRIVATE_URLS") == "1"
OAUTH = os.environ.get("OAUTH", "1") != "0"
FILE_TYPES = {".stl": "model/stl", ".obj": "model/obj", ".mtl": "text/plain", ".zip": "application/zip"}

store = Store(DATA, allow_script=ALLOW_SCRIPT)

mcp = FastMCP(
    "stl-maker",
    instructions=(
        "Creates and adjusts 3D models for printing (mm, z up). Each model is a recipe "
        "with version history and is shown live in a web UI (viewer_url) where the user "
        "can rotate the model. Workflow: create_model (CSG spec) or create_enclosure → "
        "render_model to see the result yourself → update_model with small changes (e.g. "
        "{'wall': 3} or {'/root/children/1/d': 8}) until the user is satisfied. Use "
        "variables in 'vars' for dimensions likely to be adjusted, and the templates (screw_hole, "
        "heat_insert, boss, snap_hook, hinge, text …) instead of building such things yourself. "
        "get_model shows the recipe and the user's comments: they are points the user "
        "clicked in the 3D view, with near_nodes = the nodes in the recipe that are closest. "
        "Resolve them with update_model(..., resolves=[id]) or answer with reply_to_comment. "
        "list_models shows open_comments per model. Always give the user the viewer_url. "
        "Every build runs a print check; problems (not watertight, thin walls, overhangs, too big "
        "for the bed ...) come back as print_warnings. Fix them when you can, otherwise tell the "
        "user; check_print re-checks a model or file with the current settings. "
        "Tinkercad: import_file takes in STL/OBJ exported from Tinkercad (standalone file), "
        "modify_file builds on a file, export_obj makes a colored OBJ with several parts "
        "(also from the model library) that can be imported into Tinkercad."),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ---------------------------------------------------------------- helpers

def _summary(m: dict, full: bool = False) -> dict:
    cur = Store.current(m)
    out = {
        "model_id": m["id"],
        "name": m["name"],
        "version": cur["version"],
        "kind": cur["kind"],
        "viewer_url": f"{PUBLIC_URL}/m/{m['id']}",
        "parts": {n: {"url": f"{PUBLIC_URL}/files/{m['id']}/{p['file']}",
                      "extents_mm": p["extents_mm"], "volume_cm3": p["volume_cm3"],
                      "watertight": p["watertight"], "warnings": p.get("warnings", [])}
                  for n, p in cur["parts"].items()},
    }
    flat = [f"{n}: {w['message']}" for n, p in cur["parts"].items() for w in p.get("warnings", [])]
    if flat:
        out["print_warnings"] = flat
    if full:
        out["description"] = m.get("description", "")
        out["params"] = cur["params"]
        out["history"] = [{"version": v["version"], "note": v["note"]} for v in m["versions"][-15:]]
        comments = m.get("comments", [])
        out["comments"] = [_comment_view(c) for c in comments if c["status"] == "open"]
        out["resolved_comments"] = sum(c["status"] != "open" for c in comments)
    return out


def _comment_view(c: dict) -> dict:
    keep = ("id", "text", "part", "point", "normal", "version", "near_nodes", "inner_xy",
            "inner_xyz", "replies")
    return {k: c[k] for k in keep if k in c and c[k] not in (None, [])}


async def _run(fn, *a, **kw):
    """Runs heavy builds in a thread so the server (and web UI) stays responsive."""
    return await anyio.to_thread.run_sync(lambda: fn(*a, **kw))


def _open(m: dict) -> int:
    return sum(c["status"] == "open" for c in m.get("comments", []))


# ---------------------------------------------------------------- MCP tools

CREATE_MODEL_DOC = """Creates a new parametric model from a CSG spec (JSON). Returns model_id and viewer_url.

Put dimensions that may change in "vars" and reference them with "=expression", so later
adjustments are easy: update_model(model_id, {"width": 60}).

""" + csg.SPEC_HELP


@mcp.tool(description=CREATE_MODEL_DOC)
async def create_model(name: str, spec: dict, description: str = "") -> dict:
    m = await _run(store.create, name, "csg", spec, description)
    return _summary(m)


@mcp.tool()
async def create_enclosure(
    name: str,
    inner_x: float, inner_y: float, inner_z: float,
    wall: float = 2.4, floor: float = 2.4, lid_thickness: float = 2.4,
    corner_screws: bool = True,
    standoffs: list[dict] | None = None,
    cutouts: list[dict] | None = None,
    lid_holes: list[dict] | None = None,
    lid_slots: list[dict] | None = None,
    description: str = "",
) -> dict:
    """Creates a box + lid (parts 'body' and 'lid'). Dimensions in mm.

    Inner coordinates have their origin at the box's lower left inner corner (x along, y across).
    standoffs: [{x, y, hole_d, height=4, outer_d=5}]  hole centers for standoffs
    cutouts:   [{wall: left|right|front|back, pos, width, height, z=0}]  pos = center along the wall
    lid_holes: [{x, y, d}]   lid_slots: [{x, y, w, h}]
    corner_screws: 4 corner posts for M3 self-tapping screws.
    Adjust later with update_model(model_id, {"inner_z": 30, "/cutouts/0/width": 14}).
    """
    params = {"inner_x": inner_x, "inner_y": inner_y, "inner_z": inner_z, "wall": wall,
              "floor": floor, "lid_thickness": lid_thickness, "corner_screws": corner_screws,
              "standoffs": standoffs or [], "cutouts": cutouts or [], "lid_holes": lid_holes or [],
              "lid_slots": lid_slots or []}
    m = await _run(store.create, name, "enclosure", params, description)
    return _summary(m)


@mcp.tool()
async def create_from_script(name: str, code: str, description: str = "") -> dict:
    """Advanced: Python that sets `result` (trimesh.Trimesh), `parts` (list -> union) or
    `parts` (dict name->mesh). Available: trimesh, numpy as np, g (blk/cyl/union/diff).
    Requires ALLOW_SCRIPT=1. Prefer create_model – it is safe and easier to adjust."""
    m = await _run(store.create, name, "script", {"code": code}, description)
    return _summary(m)


@mcp.tool()
async def update_model(model_id: str, changes: dict | None = None, remove: list[str] | None = None,
                       params: dict | None = None, note: str = "", name: str | None = None,
                       description: str | None = None, resolves: list[str] | None = None) -> dict:
    """Adjusts a model and creates a new version (the web view updates immediately).

    changes: {"key": value} – for CSG models this sets vars[key], otherwise the parameter.
             {"/json/pointer": value} – sets any value in the recipe, e.g.
             {"/root/children/2/translate/0": 15} or {"/root/children/-": {...}} (append).
    remove:  list of JSON pointers to remove, e.g. ["/root/children/3"].
    params:  replace the whole recipe (spec for CSG, {"code": ...} for script).
    note:    short description of the change (shown in the history).
    resolves: comment ids (e.g. ["c2"]) that the change addresses – marked resolved with a reply.
    Read the recipe with get_model first if you are unsure of the structure.
    """
    m = await _run(store.update, model_id, params=params, changes=changes, remove=remove,
                   note=note, name=name, description=description, resolves=resolves)
    return _summary(m)


@mcp.tool()
def get_model(model_id: str) -> dict:
    """Gets the model's current recipe (params), dimensions per part, version history and open
    comments. A comment has text, point (mm, in the part's coordinates = the STL file's), part,
    version and near_nodes (JSON pointers to the nearest nodes in the recipe)."""
    return _summary(store.get(model_id), full=True)


@mcp.tool()
def reply_to_comment(model_id: str, comment_id: str, text: str, resolve: bool = False) -> dict:
    """Replies to a comment from the web UI (e.g. a follow-up question), and marks it resolved if resolve=True.
    The reply shows up immediately in the 3D view. Use update_model(..., resolves=[id]) when you have changed the model."""
    m = store.comment_action(model_id, comment_id, "resolve" if resolve else "reply", text,
                             author="claude")
    c = Store._comment(m, comment_id)
    return {**_comment_view(c), "status": c["status"]}


@mcp.tool()
async def render_model(model_id: str, version: int | None = None,
                       views: list[str] | None = None) -> list:
    """Renders the model to an image so you can inspect it visually.
    views: choose from iso, front, top, right, back_iso (default iso, front, top, right)."""
    m = store.get(model_id)
    v = Store.version(m, version) if version else Store.current(m)
    if v.get("pruned"):
        raise ValueError("the version's files have been pruned – use revert_model first")
    vw = tuple(x for x in (views or ["iso", "front", "top", "right"]) if x in render.VIEWS) or ("iso",)

    def work():
        import trimesh
        parts = {n: trimesh.load(store.file(model_id, p["file"])) for n, p in v["parts"].items()}
        return render.render_png(parts, vw)
    png = await _run(work)
    info = _summary(m)
    info["rendered_version"] = v["version"]
    return [json.dumps(info, ensure_ascii=False), Image(data=png, format="png")]


@mcp.tool()
def list_models() -> list[dict]:
    """Lists all models in the library (most recently changed first)."""
    out = []
    for m in store.list():
        cur = Store.current(m)
        out.append({"model_id": m["id"], "name": m["name"], "kind": cur["kind"],
                    "version": cur["version"], "parts": list(cur["parts"]),
                    "open_comments": _open(m), "viewer_url": f"{PUBLIC_URL}/m/{m['id']}"})
    return out


@mcp.tool()
async def revert_model(model_id: str, version: int) -> dict:
    """Restores the model to an earlier version (created as a new version)."""
    return _summary(await _run(store.revert, model_id, version))


@mcp.tool()
async def duplicate_model(model_id: str, name: str) -> dict:
    """Copies a model to a new one (to try a variant without touching the original)."""
    return _summary(await _run(store.duplicate, model_id, name))


@mcp.tool()
def delete_model(model_id: str) -> str:
    """Deletes a model and its entire history."""
    store.delete(model_id)
    return f"deleted {model_id}"


# ---------------------------------------------------------------- standalone files (Tinkercad etc.)

def _safe(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    if not n:
        raise ValueError("invalid name")
    return n[:60]


def _url(name: str) -> str:
    return f"{PUBLIC_URL}/files/{name}"


def _save(mesh, name: str) -> dict:
    path = OUT / f"{name}.stl"
    mesh.export(path)
    info = {
        "file": path.name,
        "url": _url(path.name),
        "watertight": bool(mesh.is_watertight),
        "extents_mm": [round(float(v), 2) for v in mesh.extents],
        "bounds_mm": [[round(float(v), 2) for v in b] for b in mesh.bounds],
        "volume_cm3": round(float(mesh.volume) / 1000, 2),
        "triangles": len(mesh.faces),
        "size_bytes": path.stat().st_size,
    }
    warnings = printcheck.check(mesh, store.settings.get())
    if warnings:
        info["print_warnings"] = [w["message"] for w in warnings]
    if info["size_bytes"] > TINKERCAD_MAX:
        small = g.simplify_to(mesh, TINKERCAD_MAX)
        sp = OUT / f"{name}_tinkercad.stl"
        small.export(sp)
        info["tinkercad"] = {"note": f"over Tinkercad's limit ({TINKERCAD_MAX / 1e6:g} MB), "
                                     "simplified copy created",
                             "file": sp.name, "url": _url(sp.name), "triangles": len(small.faces),
                             "size_bytes": sp.stat().st_size}
    return info


def _out_file(file: str, suffixes=tuple(FILE_TYPES)) -> Path:
    p = OUT / Path(file).name
    if p.suffix not in suffixes or not p.is_file():
        raise FileNotFoundError(file)
    return p


def _load(file: str):
    """Flat file ('box.stl') or a model's file ('<model_id>/v2/body.stl')."""
    if "/" in file:
        mid, rel = file.split("/", 1)
        return g.load_mesh(store.file(mid, rel).read_bytes(), "stl")
    return g.load_mesh(_out_file(file, (".stl",)).read_bytes(), "stl")


def _check_public(host: str):
    if ALLOW_PRIVATE_URLS:
        return
    for info in socket.getaddrinfo(host, None):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise PermissionError(f"{host} points to a private address ({ip}); set ALLOW_PRIVATE_URLS=1")


def _fetch(url: str) -> tuple[bytes, str]:
    """Fetches url (max MAX_UPLOAD, max 5 redirects, public addresses only).
    Returns (data, file extension from the final url)."""
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for _ in range(6):
            u = urlparse(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise ValueError("only http(s) links are supported")
            _check_public(u.hostname)
            with client.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers["location"])
                    continue
                r.raise_for_status()
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_UPLOAD:
                        raise ValueError(f"the file is larger than {MAX_UPLOAD // 1_000_000} MB")
                return bytes(buf), Path(u.path).suffix.lstrip(".")
    raise ValueError("too many redirects")


@mcp.tool()
def import_file(name: str, url: str = "", data_base64: str = "", format: str = "") -> dict:
    """Imports a model (e.g. exported from Tinkercad) and saves it as <name>.stl.
    Provide url (http/https) OR data_base64. format: stl, obj, glb, ply, off; guessed from the url if
    empty. Zip with obj+mtl (Tinkercad's OBJ export) is unpacked. Dimensions are assumed to be mm.
    The user can also upload directly: curl -T file.stl -H "Authorization: Bearer ..." <server>/upload/file.stl"""
    if bool(url) == bool(data_base64):
        raise ValueError("provide either url or data_base64")
    if url:
        data, ext = _fetch(url)
        format = format or ext
    else:
        data = base64.b64decode(data_base64, validate=True)
        if len(data) > MAX_UPLOAD:
            raise ValueError(f"the file is larger than {MAX_UPLOAD // 1_000_000} MB")
    return _save(g.load_mesh(data, format or "stl"), _safe(name))


@mcp.tool()
def file_info(file: str) -> dict:
    """Dimensions, bounds, volume and watertightness of an STL file on the server."""
    mesh = _load(file)
    return {"file": Path(file).name, "watertight": bool(mesh.is_watertight),
            "extents_mm": [round(float(v), 2) for v in mesh.extents],
            "bounds_mm": [[round(float(v), 2) for v in b] for b in mesh.bounds],
            "volume_cm3": round(float(mesh.volume) / 1000, 2), "triangles": len(mesh.faces),
            "parts": len(mesh.split(only_watertight=False))}


@mcp.tool()
def check_print(model_id: str | None = None, file: str | None = None) -> dict:
    """Print check (watertight, disconnected shells, thin walls, overhangs, build volume) of a
    library model's current version (model_id) or a standalone STL file (file), using the
    thresholds configured in the admin UI. Warnings only - nothing is changed."""
    if bool(model_id) == bool(file):
        raise ValueError("give exactly one of model_id or file")
    settings = store.settings.get()
    if file:
        warnings = {"model": printcheck.check(_load(file), settings)}
    else:
        import trimesh
        cur = Store.current(store.get(model_id))
        warnings = {n: printcheck.check(trimesh.load(store.file(model_id, p["file"]), force="mesh"),
                                        settings) for n, p in cur["parts"].items()}
    return {"ok": not any(warnings.values()), "warnings": warnings, "settings": settings}


@mcp.tool()
def modify_file(file: str, name: str = "", scale: float | list[float] | None = None,
                rotate: list[float] | None = None, mirror: str = "", place_on_bed: bool = True,
                center_xy: bool = False, add: list[dict] | None = None,
                subtract: list[dict] | None = None, repair: bool = False,
                max_triangles: int = 0) -> dict:
    """Builds on an existing STL (e.g. imported from Tinkercad). Saved as <name>.stl
    (default: overwrites the file). Order: repair, scale, rotate, mirror, center_xy/place_on_bed,
    add, subtract, max_triangles.
    scale: number or [sx,sy,sz]. rotate: [rx,ry,rz] degrees around the model's center. mirror: e.g. "x".
    add/subtract: [{shape: box|cylinder|sphere|cone|tube, x,y,z (box) | diameter, inner_diameter,
    height, at: [x,y,z] = center of the shape in model coordinates AFTER placement,
    rotate: [rx,ry,rz]}]. E.g. screw hole: {shape: cylinder, diameter: 3.4, height: 20, at: [10,10,5]}.
    Check bounds_mm with file_info first. add/subtract require a watertight model.
    max_triangles: simplify the mesh to at most this many triangles."""
    mesh = g.modify(_load(file), scale=scale, rotate=rotate, mirror=mirror,
                    place_on_bed=place_on_bed, center_xy=center_xy, add=add, subtract=subtract,
                    do_repair=repair)
    if max_triangles and len(mesh.faces) > max_triangles:
        mesh = mesh.simplify_quadric_decimation(face_count=max_triangles)
    return _save(mesh, _safe(name or Path(file).stem))


@mcp.tool()
def split_file(file: str) -> list[dict]:
    """Splits an STL into its loose parts: <name>_part1.stl, <name>_part2.stl, ... (largest first)."""
    parts = sorted(_load(file).split(only_watertight=False), key=lambda m: -len(m.faces))
    stem = Path(file).stem
    return [_save(p, f"{stem}_part{i}") for i, p in enumerate(parts, 1)]


@mcp.tool()
def export_obj(name: str, parts: list[dict]) -> dict:
    """Merges STL files into one colored OBJ (one object + color per part) for Tinkercad etc.
    parts: [{file: 'box_body.stl', color: 'red' | '#rrggbb' (optional)}]. Parts from the model
    library are given as '<model_id>/<file>' (file per parts[...].url in get_model, e.g. 'box/v3/lid.stl').
    Creates <name>.obj, <name>.mtl and <name>.zip (both files; import the zip's contents)."""
    name = _safe(name)
    loaded = [(_safe(Path(p["file"]).stem), _load(p["file"]), p.get("color", "")) for p in parts]
    obj, mtl = g.to_obj(loaded, f"{name}.mtl")
    if len(obj) > TINKERCAD_MAX:
        ratio = TINKERCAD_MAX / len(obj) * 0.9
        loaded = [(n, m.simplify_quadric_decimation(face_count=max(4, int(len(m.faces) * ratio))), c)
                  for n, m, c in loaded]
        obj, mtl = g.to_obj(loaded, f"{name}.mtl")
    (OUT / f"{name}.obj").write_text(obj)
    (OUT / f"{name}.mtl").write_text(mtl)
    with zipfile.ZipFile(OUT / f"{name}.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}.obj", obj)
        z.writestr(f"{name}.mtl", mtl)
    return {"obj": _url(f"{name}.obj"), "mtl": _url(f"{name}.mtl"), "zip": _url(f"{name}.zip"),
            "parts": [n for n, _, _ in loaded], "size_bytes": len(obj),
            "triangles": sum(len(m.faces) for _, m, _ in loaded)}


@mcp.tool()
def list_files() -> list[dict]:
    """Lists all files (stl, obj, mtl, zip) on the server with links."""
    return [{"file": p.name, "url": _url(p.name), "size_bytes": p.stat().st_size}
            for p in sorted(OUT.iterdir()) if p.suffix in FILE_TYPES and p.is_file()]


@mcp.tool()
def delete_file(file: str) -> str:
    """Deletes a file (filename only, e.g. 'box_lid.stl')."""
    p = _out_file(file)
    p.unlink()
    return f"deleted {p.name}"


# ---------------------------------------------------------------- auth
#
# Ways in: Authorization: Bearer <MCP_TOKEN or an OAuth access token>, ?token=<MCP_TOKEN> (sets a
# session cookie) or the session cookie set after a passkey login. See auth.py for the OAuth flow.

oauth = OAuthProvider(AuthState(DATA / "auth.json"))
challenges = Expiring(CHALLENGE_TTL)  # WebAuthn challenges per login/registration attempt
SESSION_COOKIE = "stl_session"
PASSKEY_LOGIN_PATHS = ("/auth/passkey/login/options", "/auth/passkey/login/verify")
OAUTH_PATHS = ("/authorize", "/token", "/register", "/revoke")


def _oauth_routes() -> list:
    if not OAUTH:
        return []
    try:
        issuer = AnyHttpUrl(PUBLIC_URL)
        routes = create_auth_routes(
            oauth, issuer_url=issuer,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True))
    except ValueError as e:
        print(f"OAuth disabled: {e} (PUBLIC_URL={PUBLIC_URL})", flush=True)
        return []
    prm = create_protected_resource_routes(AnyHttpUrl(f"{PUBLIC_URL}/mcp"), [issuer],
                                           scopes_supported=[SCOPE], resource_name="STL library")
    # Some clients look for the resource metadata at the root instead of under /mcp.
    root_prm = Route("/.well-known/oauth-protected-resource", prm[0].endpoint, methods=["GET", "OPTIONS"])
    return routes + prm + [root_prm]


OAUTH_ROUTES = _oauth_routes()
RESOURCE_METADATA_URL = str(build_resource_metadata_url(AnyHttpUrl(f"{PUBLIC_URL}/mcp")))


def _eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _bearer(request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def _token_ok(request) -> bool:
    bearer = _bearer(request)
    if _eq(bearer, TOKEN) or (bearer and OAUTH_ROUTES and oauth.access_token_ok(bearer)):
        return True
    return (_eq(request.query_params.get("token", ""), TOKEN)
            or check_session(TOKEN, request.cookies.get(SESSION_COOKIE, "")))


def _set_session(resp: Response) -> Response:
    resp.set_cookie(SESSION_COOKIE, make_session(TOKEN), max_age=SESSION_TTL, httponly=True,
                    samesite="lax", secure=PUBLIC_URL.startswith("https"))
    resp.delete_cookie("stl_token")  # the old cookie held the raw token
    return resp


def _public_path(path: str, read: bool) -> bool:
    if path == "/admin" or path.startswith(("/admin/", "/api/settings")) or path == "/api/recheck":
        return False  # admin is never public, not even with UI_PUBLIC
    return (path == "/health" or path.startswith("/static/") or path in PASSKEY_LOGIN_PATHS
            or (bool(OAUTH_ROUTES) and (path in OAUTH_PATHS or path.startswith("/.well-known/oauth-")))
            or (FILES_PUBLIC and read and path.startswith("/files/"))
            or (UI_PUBLIC and read and (path == "/" or path.startswith("/m/")
                                        or (path.startswith("/api/")
                                            and not path.startswith("/api/auth")))))


class Auth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not TOKEN:
            return await call_next(request)
        path, method = request.url.path, request.method
        ok = _token_ok(request)
        read = method in ("GET", "HEAD")
        if not ok and not _public_path(path, read):
            if read and "text/html" in request.headers.get("accept", ""):
                return HTMLResponse((WEB / "login.html").read_text(), status_code=401,
                                    headers={"Cache-Control": "no-store"})
            headers = {}
            if OAUTH_ROUTES and path.startswith("/mcp"):
                # Tells OAuth-capable MCP clients (claude.ai) where to start the login.
                headers["WWW-Authenticate"] = f'Bearer resource_metadata="{RESOURCE_METADATA_URL}"'
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=headers)
        if ok and read and request.query_params.get("token") and not path.startswith("/mcp"):
            # login via ?token= : set a session cookie and remove the token from the URL
            rest = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "token"])
            return _set_session(RedirectResponse(path + (f"?{rest}" if rest else ""), status_code=303))
        return await call_next(request)


# ---- passkeys (WebAuthn)

def _rp() -> tuple[str, str]:
    """(relying party id, expected origin) - both follow PUBLIC_URL."""
    u = urlparse(PUBLIC_URL)
    return u.hostname or "localhost", f"{u.scheme}://{u.netloc}"


def _passkey_view(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "name", "created", "last_used")}


def _options(purpose: str, opts) -> JSONResponse:
    flow = challenges.put(secrets.token_urlsafe(16), (purpose, opts.challenge))
    return JSONResponse({"flow": flow, "options": json.loads(webauthn.options_to_json(opts))})


async def _flow(request, purpose: str) -> tuple[dict, bytes | None]:
    body = await request.json()
    entry = challenges.pop(str(body.get("flow", "")))
    return body, entry[1] if entry and entry[0] == purpose else None


async def passkey_login_options(_):
    return _options("login", webauthn.generate_authentication_options(
        rp_id=_rp()[0], user_verification=UserVerificationRequirement.PREFERRED))


async def passkey_login_verify(request):
    body, challenge = await _flow(request, "login")
    cred = body.get("credential") or {}
    pk = oauth.state.passkey(str(cred.get("id", ""))) if isinstance(cred, dict) else None
    if not challenge or not pk:
        return JSONResponse({"error": "unknown passkey or expired attempt - try again"}, status_code=400)
    rp_id, origin = _rp()
    try:
        v = webauthn.verify_authentication_response(
            credential=cred, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=webauthn.base64url_to_bytes(pk["public_key"]),
            credential_current_sign_count=pk["sign_count"])
    except Exception as e:  # noqa: BLE001 – the reason is shown on the login page
        return JSONResponse({"error": f"passkey verification failed: {e}"}, status_code=400)
    oauth.state.touch_passkey(pk["id"], v.new_sign_count)
    return _set_session(JSONResponse({"ok": True}))


async def passkey_register_options(_):
    rp_id, _origin = _rp()
    existing = [PublicKeyCredentialDescriptor(id=webauthn.base64url_to_bytes(p["id"]))
                for p in oauth.state.passkeys()]
    return _options("register", webauthn.generate_registration_options(
        rp_id=rp_id, rp_name="STL library", user_id=b"stl-mcp-owner", user_name="owner",
        user_display_name="STL library", exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED)))


async def passkey_register_verify(request):
    body, challenge = await _flow(request, "register")
    if not challenge:
        return JSONResponse({"error": "the attempt expired - try again"}, status_code=400)
    rp_id, origin = _rp()
    try:
        v = webauthn.verify_registration_response(
            credential=body.get("credential"), expected_challenge=challenge,
            expected_rp_id=rp_id, expected_origin=origin)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"passkey registration failed: {e}"}, status_code=400)
    pk = oauth.state.add_passkey(bytes_to_base64url(v.credential_id),
                                 bytes_to_base64url(v.credential_public_key), v.sign_count,
                                 str(body.get("name") or ""))
    return JSONResponse({"ok": True, "passkey": _passkey_view(pk)})


async def logout(_):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie("stl_token")
    return resp


async def api_auth(_):
    return JSONResponse({"passkeys": [_passkey_view(p) for p in oauth.state.passkeys()],
                         "clients": oauth.state.clients() if OAUTH_ROUTES else [],
                         "oauth": bool(OAUTH_ROUTES), "mcp_url": f"{PUBLIC_URL}/mcp",
                         "login_required": bool(TOKEN)})


async def api_auth_passkey(request):
    if not oauth.state.delete_passkey(request.path_params["id"]):
        return JSONResponse({"error": "no such passkey"}, status_code=404)
    return JSONResponse({"ok": True})


async def api_auth_client(request):
    if not oauth.state.revoke_client(request.path_params["id"]):
        return JSONResponse({"error": "no such app"}, status_code=404)
    return JSONResponse({"ok": True})


# ---- OAuth consent (the user is logged in to the web UI by the time this runs)

def _consent_page(body: str, status: int = 200) -> HTMLResponse:
    page = (WEB / "consent.html").read_text().replace("{{body}}", body)
    return HTMLResponse(page, status_code=status, headers={
        "Cache-Control": "no-store", "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'"})


EXPIRED = "<h2>The request has expired</h2><p>Start the connection again from the app.</p>"


async def oauth_consent(request):
    if request.method == "GET":
        req = request.query_params.get("req", "")
        p = oauth.pending_request(req)
        if not p:
            return _consent_page(EXPIRED, 400)
        dest = urlparse(p["params"]["redirect_uri"])
        e = html.escape
        return _consent_page(
            f"<h2>Connect {e(p['client_name'])}?</h2>"
            f"<p>It will be able to read, create and change the models in this STL library.</p>"
            f"<p class=\"hint\">After approving you are sent back to "
            f"<strong>{e(dest.scheme)}://{e(dest.netloc)}</strong>. Only approve if you just "
            f"started this from that app.</p>"
            f"<form method=\"post\" class=\"row\" style=\"justify-content:center\">"
            f"<input type=\"hidden\" name=\"req\" value=\"{e(req)}\">"
            f"<button class=\"primary\" name=\"decision\" value=\"allow\">Allow</button>"
            f"<button name=\"decision\" value=\"deny\">Deny</button></form>")
    origin = request.headers.get("origin")
    if origin and origin != _rp()[1]:
        return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
    form = await request.form()
    req = str(form.get("req", ""))
    if form.get("decision") == "allow":
        try:
            url = oauth.approve(req)
        except AuthorizeError:
            return _consent_page(EXPIRED, 400)
    else:
        url = oauth.deny(req)
        if not url:
            return _consent_page(EXPIRED, 400)
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------- web

def _err(e: Exception, code=400):
    return JSONResponse({"error": str(e)}, status_code=404 if isinstance(e, KeyError) else code)


async def page_index(_):
    return FileResponse(WEB / "index.html")


async def page_model(_):
    return FileResponse(WEB / "viewer.html")


async def api_models(_):
    out = []
    for m in store.list():
        cur = Store.current(m)
        out.append({"id": m["id"], "name": m["name"], "description": m.get("description", ""),
                    "updated": m["updated"], "version": cur["version"], "kind": cur["kind"],
                    "versions": len(m["versions"]), "open_comments": _open(m),
                    "warnings": sum(len(p.get("warnings", [])) for p in cur["parts"].values()),
                    "parts": {n: {"url": f"/files/{m['id']}/{p['file']}",
                                  "extents_mm": p["extents_mm"]} for n, p in cur["parts"].items()}})
    return JSONResponse({"models": out, "revision": store.revision})


async def api_model(request):
    mid = request.path_params["id"]
    try:
        if request.method == "DELETE":
            store.delete(mid)
            return JSONResponse({"ok": True})
        if request.method == "POST":
            body = await request.json()
            action = body.get("action")
            if action == "set_current":
                m = store.set_current(mid, int(body["version"]))
            elif action == "revert":
                m = await _run(store.revert, mid, int(body["version"]))
            elif action == "rename":
                m = store.update(mid, name=body.get("name"), description=body.get("description"))
            elif action == "update":
                m = await _run(store.update, mid, params=body.get("params"),
                               changes=body.get("changes"), note=body.get("note", "from the web UI"))
            elif action == "comment_add":
                await _run(store.add_comment, mid, body.get("text", ""), body.get("point"),
                           body.get("normal"), body.get("part"), body.get("version"))
                m = store.get(mid)
            elif action in ("comment_resolve", "comment_reopen", "comment_reply", "comment_delete"):
                m = store.comment_action(mid, body["id"], action.removeprefix("comment_"),
                                         body.get("text", ""))
            elif action == "duplicate":
                m = await _run(store.duplicate, mid, body.get("name") or f"{mid} copy")
            else:
                return JSONResponse({"error": "unknown action"}, status_code=400)
            return JSONResponse({"ok": True, "id": m["id"]})
        m = store.get(mid)
    except Exception as e:  # noqa: BLE001 – the error is shown in the UI
        return _err(e)
    for v in m["versions"]:
        for p in v["parts"].values():
            p["url"] = f"/files/{m['id']}/{p['file']}"
    m["revision"] = store.revision
    return JSONResponse(m)


async def page_admin(_):
    return FileResponse(WEB / "admin.html")


async def page_admin_design(_):
    """The design system reference (docs/DESIGN.md), rendered from the live style.css."""
    return FileResponse(WEB / "design.html")


async def api_settings(request):
    """GET: values + defaults + schema. POST {values: {...}} saves; POST {reset: true | [keys]} resets."""
    try:
        if request.method == "POST":
            body = await request.json()
            if "reset" in body:
                r = body["reset"]
                store.settings.reset(None if r is True else list(r))
            if body.get("values"):
                store.settings.update(body["values"])
    except Exception as e:  # noqa: BLE001 – the error is shown in the UI
        return _err(e)
    return JSONResponse(store.settings.describe())


async def api_recheck(request):
    """Re-run the print checks on the current version of every model with the current settings."""
    n = await _run(store.recheck)
    return JSONResponse({"ok": True, "models": n})


async def api_events(request):
    """Server-sent events: sends {revision, id} whenever something in the library changes."""
    async def gen():
        last = store.revision
        yield f"data: {json.dumps({'revision': last})}\n\n"
        idle = 0
        while not await request.is_disconnected():
            await asyncio.sleep(0.5)
            idle += 1
            if store.revision != last:
                last = store.revision
                idle = 0
                yield f"data: {json.dumps({'revision': last, 'id': store.last_changed})}\n\n"
            elif idle >= 30:
                idle = 0
                yield ": ping\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def files(request):
    try:
        p = store.file(request.path_params["id"], request.path_params["rel"])
    except KeyError:
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(p, media_type="model/stl", filename=f"{request.path_params['id']}_{p.name}",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def preview(request):
    """PNG preview (the same rendering Claude sees)."""
    mid = request.path_params["id"]
    try:
        m = store.get(mid)
        v = Store.current(m)
        cache = store.dir / mid / f"v{v['version']}" / "preview.png"
        if cache.is_file():
            return FileResponse(cache, media_type="image/png")

        def work():
            import trimesh
            parts = {n: trimesh.load(store.file(mid, p["file"])) for n, p in v["parts"].items()}
            png = render.render_png(parts, ("iso",), size=360, footer_text=False)
            cache.write_bytes(png)
            return png
        png = await _run(work)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    return Response(png, media_type="image/png")


async def flat_files(request):
    p = OUT / Path(request.path_params["name"]).name
    if p.suffix not in FILE_TYPES or not p.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(p, media_type=FILE_TYPES[p.suffix], filename=p.name)


async def upload(request):
    """PUT/POST /upload/<file.stl|obj|glb|ply|off|zip> with the file as body -> <file>.stl"""
    fname = Path(request.path_params["name"])
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD:
            return JSONResponse({"error": f"larger than {MAX_UPLOAD // 1_000_000} MB"}, status_code=413)
    try:
        mesh = g.load_mesh(bytes(body), fname.suffix.lstrip(".") or "stl")
        return JSONResponse(_save(mesh, _safe(fname.stem)))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def health(_):
    return PlainTextResponse("ok")


mcp_app = mcp.streamable_http_app()
app = Starlette(
    routes=[
        Route("/", page_index),
        Route("/m/{id}", page_model),
        Route("/api/models", api_models),
        Route("/api/models/{id}", api_model, methods=["GET", "POST", "DELETE"]),
        Route("/api/models/{id}/preview.png", preview),
        Route("/api/events", api_events),
        Route("/admin", page_admin),
        Route("/admin/design", page_admin_design),
        Route("/api/settings", api_settings, methods=["GET", "POST"]),
        Route("/api/recheck", api_recheck, methods=["POST"]),
        Route("/files/{name}", flat_files),
        Route("/files/{id}/{rel:path}", files),
        Route("/upload/{name}", upload, methods=["PUT", "POST"]),
        Route("/health", health),
        Route("/auth/passkey/login/options", passkey_login_options, methods=["POST"]),
        Route("/auth/passkey/login/verify", passkey_login_verify, methods=["POST"]),
        Route("/auth/passkey/register/options", passkey_register_options, methods=["POST"]),
        Route("/auth/passkey/register/verify", passkey_register_verify, methods=["POST"]),
        Route("/auth/logout", logout, methods=["POST"]),
        Route("/api/auth", api_auth),
        Route("/api/auth/passkeys/{id}", api_auth_passkey, methods=["DELETE"]),
        Route("/api/auth/clients/{id}", api_auth_client, methods=["DELETE"]),
        Route("/oauth/consent", oauth_consent, methods=["GET", "POST"]),
        *OAUTH_ROUTES,
        Mount("/static", app=RevalidatedStatic(directory=WEB)),
        Mount("/", app=mcp_app),
    ],
    lifespan=mcp_app.router.lifespan_context,
)
app.add_middleware(Auth)

# Startup marker for the deploy: .github/workflows/deploy.yml does not accept a deploy
# until exactly this line, with the commit that was built, shows up in the container log.
# GIT_SHA is baked in via a build-arg in the Dockerfile. If the text changes here,
# READY_MARKER in deploy.yml must change too.
READY_MARKER = "STL_MCP_READY"


def ready_line(port: int) -> str:
    return f"{READY_MARKER} sha={os.environ.get('GIT_SHA') or 'unknown'} port={port}"


class Server(uvicorn.Server):
    # uvicorn runs the lifespan startup (MCP's session manager) BEFORE binding, so the
    # marker is printed only once both are done - i.e. when the port is actually listening.
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            print(ready_line(self.config.port), flush=True)


if __name__ == "__main__":
    Server(uvicorn.Config(app, host=os.environ.get("HOST", "0.0.0.0"),
                          port=int(os.environ.get("PORT", "8765")))).run()
