"""STL-MCP: MCP-server (streamable HTTP) som skapar STL-filer och serverar dem för nedladdning.

Miljövariabler:
  STL_DIR        katalog för filer              (default ./out)
  PUBLIC_URL     bas-URL som används i länkar   (default http://localhost:8765)
  MCP_TOKEN      bearer-token; krävs om satt (rekommenderas starkt)
  FILES_PUBLIC   1 = /files/* kräver ingen token (länkar går att klicka)
  ALLOW_SCRIPT   1 = aktivera create_from_script (kör godtycklig Python!)
  HOST / PORT    default 0.0.0.0 / 8765
"""
import os, re, sys, json, subprocess, tempfile, textwrap
from pathlib import Path

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

mcp = FastMCP(
    "stl-maker",
    instructions=("Skapar STL-filer (mm). Använd create_enclosure för lådor med lock, "
                  "create_primitive för enkla former, list_files för att se resultat. "
                  "Varje verktyg returnerar nedladdningslänkar."),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _safe(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    if not n:
        raise ValueError("ogiltigt namn")
    return n[:60]


def _save(mesh, name: str) -> dict:
    path = OUT / f"{name}.stl"
    mesh.export(path)
    return {
        "file": path.name,
        "url": f"{PUBLIC_URL}/files/{path.name}",
        "watertight": bool(mesh.is_watertight),
        "extents_mm": [round(float(v), 2) for v in mesh.extents],
        "volume_cm3": round(float(mesh.volume) / 1000, 2),
        "size_bytes": path.stat().st_size,
    }


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
def list_files() -> list[dict]:
    """Listar alla STL-filer på servern med länkar."""
    return [{"file": p.name, "url": f"{PUBLIC_URL}/files/{p.name}", "size_bytes": p.stat().st_size}
            for p in sorted(OUT.glob("*.stl"))]


@mcp.tool()
def delete_file(file: str) -> str:
    """Raderar en STL-fil (bara filnamn, t.ex. 'lada_lock.stl')."""
    p = (OUT / Path(file).name)
    if p.suffix != ".stl" or not p.is_file():
        raise FileNotFoundError(file)
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
    if p.suffix != ".stl" or not p.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(p, media_type="model/stl", filename=p.name)


async def health(_):
    return PlainTextResponse("ok")


mcp_app = mcp.streamable_http_app()
app = Starlette(
    routes=[Route("/files/{name}", files), Route("/health", health), Mount("/", app=mcp_app)],
    lifespan=mcp_app.router.lifespan_context,
)
app.add_middleware(Auth)

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8765")))
