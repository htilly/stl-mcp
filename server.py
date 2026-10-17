"""STL-MCP: MCP server (streamable HTTP) that creates STL files and serves them for download.

Environment variables:
  STL_DIR        directory for files             (default ./out)
  PUBLIC_URL     base URL used in links         (default http://localhost:8765)
  MCP_TOKEN      bearer token; required if set (strongly recommended)
  FILES_PUBLIC   1 = /files/* requires no token (links are clickable)
  ALLOW_SCRIPT   1 = enable create_from_script (runs arbitrary Python!)
  TINKERCAD_MAX_MB  size limit for Tinkercad import (default 25); larger files get a simplified copy
  MAX_UPLOAD_MB  max size for import/upload (default 100)
  ALLOW_PRIVATE_URLS 1 = import_file may fetch from private/local addresses
  HOST / PORT    default 0.0.0.0 / 8765
"""
import os, re, sys, json, base64, ipaddress, socket, subprocess, tempfile, textwrap, zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

import numpy as np
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

import geometry as g

OUT = Path(os.environ.get("STL_DIR", "./out")).resolve()
OUT.mkdir(parents=True, exist_ok=True)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8765").rstrip("/")
TOKEN = os.environ.get("MCP_TOKEN", "")
ALLOW_SCRIPT = os.environ.get("ALLOW_SCRIPT") == "1"
FILES_PUBLIC = os.environ.get("FILES_PUBLIC") == "1"
TINKERCAD_MAX = int(float(os.environ.get("TINKERCAD_MAX_MB", "25")) * 1_000_000)
MAX_UPLOAD = int(float(os.environ.get("MAX_UPLOAD_MB", "100")) * 1_000_000)
ALLOW_PRIVATE_URLS = os.environ.get("ALLOW_PRIVATE_URLS") == "1"
FILE_TYPES = {".stl": "model/stl", ".obj": "model/obj", ".mtl": "text/plain", ".zip": "application/zip"}

mcp = FastMCP(
    "stl-maker",
    instructions=("Creates STL files (mm). Use create_enclosure for boxes with lids, "
                  "create_primitive for simple shapes, list_files to see results. "
                  "Tinkercad: import_file takes in STL/OBJ exported from Tinkercad, "
                  "modify_file builds on a file, export_obj makes a colored OBJ with several parts "
                  "that can be imported into Tinkercad. Every tool returns download links."),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


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
def create_enclosure(
    name: str,
    inner_x: float, inner_y: float, inner_z: float,
    wall: float = 2.4, floor: float = 2.4, lid_thickness: float = 2.4,
    corner_screws: bool = True,
    standoffs: list[dict] | None = None,
    cutouts: list[dict] | None = None,
    lid_holes: list[dict] | None = None,
    lid_slots: list[dict] | None = None,
) -> dict:
    """Creates a box + lid (two STL files: <name>_body.stl, <name>_lid.stl). Dimensions in mm.

    Inner coordinates have their origin at the box's lower left inner corner (x along, y across).
    standoffs: [{x, y, hole_d, height=4, outer_d=5}]  hole centers for standoffs
    cutouts:   [{wall: left|right|front|back, pos, width, height, z=0}]  pos = center along the wall
    lid_holes: [{x, y, d}]   lid_slots: [{x, y, w, h}]
    corner_screws: 4 corner posts for M3 self-tapping screws.
    """
    name = _safe(name)
    body, lid = g.enclosure(inner_x, inner_y, inner_z, wall, floor, lid_thickness, corner_screws,
                            standoffs=standoffs, cutouts=cutouts, lid_holes=lid_holes,
                            lid_slots=lid_slots)
    return {"body": _save(body, f"{name}_body"), "lid": _save(lid, f"{name}_lid")}


@mcp.tool()
def create_primitive(name: str, shape: str, x: float = 0, y: float = 0, z: float = 0,
                     diameter: float = 0, inner_diameter: float = 0, height: float = 0) -> dict:
    """Creates a simple shape. shape: box (x,y,z), cylinder (diameter,height), sphere (diameter),
    cone (diameter,height), tube (diameter,inner_diameter,height). Dimensions in mm."""
    mesh = g.primitive(shape, x=x, y=y, z=z, diameter=diameter,
                       inner_diameter=inner_diameter, height=height)
    # place on the build plate (z=0 as lowest point)
    mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
    return _save(mesh, _safe(name))


@mcp.tool()
def create_from_script(name: str, code: str) -> dict:
    """Advanced: runs Python that sets the variable `result` (trimesh.Trimesh) or `parts` (list).
    Available: trimesh, numpy as np, g (helpers blk/cyl/union/diff). Requires ALLOW_SCRIPT=1."""
    if not ALLOW_SCRIPT:
        raise PermissionError("create_from_script is disabled (set ALLOW_SCRIPT=1 on the server)")
    name = _safe(name)
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "s.py"
        script.write_text(
            "import sys, json, numpy as np, trimesh\n"
            f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
            "import geometry as g\n" + code +
            "\nm = result if 'result' in dir() else g.union(parts)\n"
            f"m.export({str(Path(td) / 'o.stl')!r})\n")
        r = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True,
                           timeout=60, cwd=td)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-1500:])
        import trimesh
        return _save(trimesh.load(Path(td) / "o.stl"), name)


@mcp.tool()
def import_file(name: str, url: str = "", data_base64: str = "", format: str = "") -> dict:
    """Imports a model (e.g. exported from Tinkercad) and saves it as <name>.stl.
    Provide url (http/https) OR data_base64. format: stl, obj, glb, ply, off; guessed from the url if
    empty. Zip with obj+mtl (Tinkercad's OBJ export) is unpacked. Dimensions are assumed to be mm.
    The user can also upload directly: curl -T fil.stl -H "Authorization: Bearer ..." <server>/upload/fil.stl"""
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
    parts: [{file: 'box_body.stl', color: 'red' | '#rrggbb' (optional)}].
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


class Auth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        open_path = request.url.path == "/health" or (FILES_PUBLIC and request.url.path.startswith("/files/"))
        if TOKEN and not open_path:
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
                or request.query_params.get("token", "")
            if supplied != TOKEN:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def files(request):
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
    routes=[Route("/files/{name}", files), Route("/upload/{name}", upload, methods=["PUT", "POST"]),
            Route("/health", health), Mount("/", app=mcp_app)],
    lifespan=mcp_app.router.lifespan_context,
)
app.add_middleware(Auth)

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8765")))
