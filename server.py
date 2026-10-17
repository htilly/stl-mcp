"""STL-MCP: MCP-server (streamable HTTP) som skapar STL-filer och serverar dem för nedladdning.

Miljövariabler:
  STL_DIR        katalog för filer              (default ./out)
  PUBLIC_URL     bas-URL som används i länkar   (default http://localhost:8765)
  MCP_TOKEN      bearer-token; krävs om satt (rekommenderas starkt)
  FILES_PUBLIC   1 = /files/* kräver ingen token (länkar går att klicka)
  ALLOW_SCRIPT   1 = aktivera create_from_script (kör godtycklig Python!)
  TINKERCAD_MAX_MB  storleksgräns för Tinkercad-import (default 25); större filer får en förenklad kopia
  MAX_UPLOAD_MB  max storlek vid import/uppladdning (default 100)
  ALLOW_PRIVATE_URLS 1 = import_file får hämta från privata/lokala adresser
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
    instructions=("Skapar STL-filer (mm). Använd create_enclosure för lådor med lock, "
                  "create_primitive för enkla former, list_files för att se resultat. "
                  "Tinkercad: import_file tar in STL/OBJ som exporterats från Tinkercad, "
                  "modify_file bygger vidare på en fil, export_obj gör en färgad OBJ med flera delar "
                  "som kan importeras i Tinkercad. Varje verktyg returnerar nedladdningslänkar."),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _safe(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    if not n:
        raise ValueError("ogiltigt namn")
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
        info["tinkercad"] = {"note": f"över Tinkercads gräns ({TINKERCAD_MAX / 1e6:g} MB), "
                                     "förenklad kopia skapad",
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
            raise PermissionError(f"{host} pekar på en privat adress ({ip}); sätt ALLOW_PRIVATE_URLS=1")


def _fetch(url: str) -> tuple[bytes, str]:
    """Hämtar url (max MAX_UPLOAD, max 5 omdirigeringar, bara publika adresser).
    Returnerar (data, filändelse ur sista url:en)."""
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for _ in range(6):
            u = urlparse(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise ValueError("bara http(s)-länkar stöds")
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
                        raise ValueError(f"filen är större än {MAX_UPLOAD // 1_000_000} MB")
                return bytes(buf), Path(u.path).suffix.lstrip(".")
    raise ValueError("för många omdirigeringar")


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
    """Skapar låda + lock (två STL-filer: <name>_botten.stl, <name>_lock.stl). Mått i mm.

    Invändiga koordinater har origo i lådans nedre vänstra innerhörn (x längs, y på tvären).
    standoffs: [{x, y, hole_d, height=4, outer_d=5}]  hålcentrum för distanser
    cutouts:   [{wall: left|right|front|back, pos, width, height, z=0}]  pos = mitten längs väggen
    lid_holes: [{x, y, d}]   lid_slots: [{x, y, w, h}]
    corner_screws: 4 hörnstolpar för M3 självgängande skruvar.
    """
    name = _safe(name)
    body, lid = g.enclosure(inner_x, inner_y, inner_z, wall, floor, lid_thickness, corner_screws,
                            standoffs=standoffs, cutouts=cutouts, lid_holes=lid_holes,
                            lid_slots=lid_slots)
    return {"botten": _save(body, f"{name}_botten"), "lock": _save(lid, f"{name}_lock")}


@mcp.tool()
def create_primitive(name: str, shape: str, x: float = 0, y: float = 0, z: float = 0,
                     diameter: float = 0, inner_diameter: float = 0, height: float = 0) -> dict:
    """Skapar en enkel form. shape: box (x,y,z), cylinder (diameter,height), sphere (diameter),
    cone (diameter,height), tube (diameter,inner_diameter,height). Mått i mm."""
    mesh = g.primitive(shape, x=x, y=y, z=z, diameter=diameter,
                       inner_diameter=inner_diameter, height=height)
    # lägg på byggplattan (z=0 som lägsta punkt)
    mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
    return _save(mesh, _safe(name))


@mcp.tool()
def create_from_script(name: str, code: str) -> dict:
    """Avancerat: kör Python som sätter variabeln `result` (trimesh.Trimesh) eller `parts` (lista).
    Tillgängligt: trimesh, numpy as np, g (box helper blk/cyl/union/diff). Kräver ALLOW_SCRIPT=1."""
    if not ALLOW_SCRIPT:
        raise PermissionError("create_from_script är avstängd (sätt ALLOW_SCRIPT=1 på servern)")
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
    """Importerar en modell (t.ex. exporterad från Tinkercad) och sparar den som <name>.stl.
    Ange url (http/https) ELLER data_base64. format: stl, obj, glb, ply, off; gissas från url om
    tomt. Zip med obj+mtl (Tinkercads OBJ-export) packas upp. Mått antas vara i mm.
    Användaren kan också ladda upp själv: curl -T fil.stl -H "Authorization: Bearer ..." <server>/upload/fil.stl"""
    if bool(url) == bool(data_base64):
        raise ValueError("ange antingen url eller data_base64")
    if url:
        data, ext = _fetch(url)
        format = format or ext
    else:
        data = base64.b64decode(data_base64, validate=True)
        if len(data) > MAX_UPLOAD:
            raise ValueError(f"filen är större än {MAX_UPLOAD // 1_000_000} MB")
    return _save(g.load_mesh(data, format or "stl"), _safe(name))


@mcp.tool()
def file_info(file: str) -> dict:
    """Mått, gränser (bounds), volym och vattentäthet för en STL-fil på servern."""
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
    """Bygger vidare på en befintlig STL (t.ex. importerad från Tinkercad). Sparas som <name>.stl
    (default: skriver över filen). Ordning: repair, scale, rotate, mirror, center_xy/place_on_bed,
    add, subtract, max_triangles.
    scale: tal eller [sx,sy,sz]. rotate: [rx,ry,rz] grader kring modellens mitt. mirror: t.ex. "x".
    add/subtract: [{shape: box|cylinder|sphere|cone|tube, x,y,z (box) | diameter, inner_diameter,
    height, at: [x,y,z] = formens mittpunkt i modellens koordinater EFTER placering,
    rotate: [rx,ry,rz]}]. Ex. skruvhål: {shape: cylinder, diameter: 3.4, height: 20, at: [10,10,5]}.
    Kolla bounds_mm med file_info först. add/subtract kräver vattentät modell.
    max_triangles: förenkla nätet till högst så många trianglar."""
    mesh = g.modify(_load(file), scale=scale, rotate=rotate, mirror=mirror,
                    place_on_bed=place_on_bed, center_xy=center_xy, add=add, subtract=subtract,
                    do_repair=repair)
    if max_triangles and len(mesh.faces) > max_triangles:
        mesh = mesh.simplify_quadric_decimation(face_count=max_triangles)
    return _save(mesh, _safe(name or Path(file).stem))


@mcp.tool()
def split_file(file: str) -> list[dict]:
    """Delar upp en STL i sina lösa delar: <namn>_del1.stl, <namn>_del2.stl, ... (största först)."""
    parts = sorted(_load(file).split(only_watertight=False), key=lambda m: -len(m.faces))
    stem = Path(file).stem
    return [_save(p, f"{stem}_del{i}") for i, p in enumerate(parts, 1)]


@mcp.tool()
def export_obj(name: str, parts: list[dict]) -> dict:
    """Slår ihop STL-filer till en färgad OBJ (ett objekt + färg per del) för Tinkercad m.fl.
    parts: [{file: 'lada_botten.stl', color: 'red' | '#rrggbb' (valfri)}].
    Skapar <name>.obj, <name>.mtl och <name>.zip (båda filerna; importera zip:ens innehåll)."""
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
    """Listar alla filer (stl, obj, mtl, zip) på servern med länkar."""
    return [{"file": p.name, "url": _url(p.name), "size_bytes": p.stat().st_size}
            for p in sorted(OUT.iterdir()) if p.suffix in FILE_TYPES and p.is_file()]


@mcp.tool()
def delete_file(file: str) -> str:
    """Raderar en fil (bara filnamn, t.ex. 'lada_lock.stl')."""
    p = _out_file(file)
    p.unlink()
    return f"raderade {p.name}"


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
    """PUT/POST /upload/<fil.stl|obj|glb|ply|off|zip> med filen som body -> <fil>.stl"""
    fname = Path(request.path_params["name"])
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD:
            return JSONResponse({"error": f"större än {MAX_UPLOAD // 1_000_000} MB"}, status_code=413)
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
