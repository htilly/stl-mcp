"""STL-MCP: MCP-server (streamable HTTP) + webbgränssnitt för parametriska 3D-modeller.

Modeller lagras som recept (kind + params) med versionshistorik. Claude skapar och justerar
modeller via MCP; webbgränssnittet visar biblioteket och en 3D-vy som uppdateras live.

Miljövariabler:
  STL_DIR        datakatalog                      (default ./out)
  PUBLIC_URL     bas-URL som används i länkar     (default http://localhost:8765)
  MCP_TOKEN      bearer-token; krävs om satt (rekommenderas starkt)
  UI_PUBLIC      1 = webbgränssnitt + filer läsbara utan token (ändringar kräver token)
  FILES_PUBLIC   1 = bara /files/* utan token (nedladdningslänkar går att klicka)
  ALLOW_SCRIPT   1 = aktivera script-modeller (kör godtycklig Python!)
  TINKERCAD_MAX_MB  storleksgräns för Tinkercad-import (default 25); större filer får en förenklad kopia
  MAX_UPLOAD_MB  max storlek vid import/uppladdning (default 100)
  ALLOW_PRIVATE_URLS 1 = import_file får hämta från privata/lokala adresser
  HOST / PORT    default 0.0.0.0 / 8765

Fristående filer (Tinkercad-import/export, modify_file m.fl.) ligger direkt i STL_DIR och nås
på /files/<namn>; modellbiblioteket ligger i STL_DIR/models och nås på /files/<id>/<version>/<del>.
"""
import asyncio
import base64
import hmac
import ipaddress
import json
import os
import re
import socket
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import anyio
import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                                 RedirectResponse, Response, StreamingResponse)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import csg
import geometry as g
import render
from store import Store

DATA = Path(os.environ.get("STL_DIR", "./out")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8765").rstrip("/")
TOKEN = os.environ.get("MCP_TOKEN", "")
ALLOW_SCRIPT = os.environ.get("ALLOW_SCRIPT") == "1"
UI_PUBLIC = os.environ.get("UI_PUBLIC") == "1"
FILES_PUBLIC = UI_PUBLIC or os.environ.get("FILES_PUBLIC") == "1"
WEB = Path(__file__).parent / "web"
OUT = DATA  # fristående filer
TINKERCAD_MAX = int(float(os.environ.get("TINKERCAD_MAX_MB", "25")) * 1_000_000)
MAX_UPLOAD = int(float(os.environ.get("MAX_UPLOAD_MB", "100")) * 1_000_000)
ALLOW_PRIVATE_URLS = os.environ.get("ALLOW_PRIVATE_URLS") == "1"
FILE_TYPES = {".stl": "model/stl", ".obj": "model/obj", ".mtl": "text/plain", ".zip": "application/zip"}

store = Store(DATA, allow_script=ALLOW_SCRIPT)

mcp = FastMCP(
    "stl-maker",
    instructions=(
        "Skapar och justerar 3D-modeller för utskrift (mm, z uppåt). Varje modell är ett recept "
        "med versionshistorik och visas live i ett webbgränssnitt (viewer_url) där användaren "
        "kan rotera modellen. Arbetsflöde: create_model (CSG-spec) eller create_enclosure → "
        "render_model för att själv se resultatet → update_model med små ändringar (t.ex. "
        "{'wall': 3} eller {'/root/children/1/d': 8}) tills användaren är nöjd. Använd "
        "variabler i 'vars' för mått som troligen kommer justeras, och mallarna (screw_hole, "
        "heat_insert, boss, snap_hook, hinge, text …) i stället för att bygga sådant själv. "
        "get_model visar receptet och användarens kommentarer: de är punkter som användaren "
        "klickat ut i 3D-vyn, med near_nodes = vilka noder i receptet som ligger närmast. "
        "Åtgärda dem med update_model(..., resolves=[id]) eller svara med reply_to_comment. "
        "list_models visar open_comments per modell. Ge alltid användaren viewer_url. "
        "Tinkercad: import_file tar in STL/OBJ som exporterats från Tinkercad (fristående fil), "
        "modify_file bygger vidare på en fil, export_obj gör en färgad OBJ med flera delar "
        "(även från modellbiblioteket) som kan importeras i Tinkercad."),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ---------------------------------------------------------------- hjälp

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
                      "watertight": p["watertight"]}
                  for n, p in cur["parts"].items()},
    }
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
    """Kör tunga byggen i tråd så servern (och webb-UI:t) förblir responsiv."""
    return await anyio.to_thread.run_sync(lambda: fn(*a, **kw))


def _open(m: dict) -> int:
    return sum(c["status"] == "open" for c in m.get("comments", []))


# ---------------------------------------------------------------- MCP-verktyg

CREATE_MODEL_DOC = """Skapar en ny parametrisk modell från en CSG-spec (JSON). Returnerar model_id och viewer_url.

Lägg mått som kan komma att ändras i "vars" och referera dem med "=uttryck", så blir senare
justeringar enkla: update_model(model_id, {"bredd": 60}).

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
    """Skapar låda + lock (delar 'botten' och 'lock'). Mått i mm.

    Invändiga koordinater har origo i lådans nedre vänstra innerhörn (x längs, y på tvären).
    standoffs: [{x, y, hole_d, height=4, outer_d=5}]  hålcentrum för distanser
    cutouts:   [{wall: left|right|front|back, pos, width, height, z=0}]  pos = mitten längs väggen
    lid_holes: [{x, y, d}]   lid_slots: [{x, y, w, h}]
    corner_screws: 4 hörnstolpar för M3 självgängande skruvar.
    Justera senare med update_model(model_id, {"inner_z": 30, "/cutouts/0/width": 14}).
    """
    params = {"inner_x": inner_x, "inner_y": inner_y, "inner_z": inner_z, "wall": wall,
              "floor": floor, "lid_thickness": lid_thickness, "corner_screws": corner_screws,
              "standoffs": standoffs or [], "cutouts": cutouts or [], "lid_holes": lid_holes or [],
              "lid_slots": lid_slots or []}
    m = await _run(store.create, name, "enclosure", params, description)
    return _summary(m)


@mcp.tool()
async def create_from_script(name: str, code: str, description: str = "") -> dict:
    """Avancerat: Python som sätter `result` (trimesh.Trimesh), `parts` (lista -> union) eller
    `parts` (dict namn->mesh). Tillgängligt: trimesh, numpy as np, g (blk/cyl/union/diff).
    Kräver ALLOW_SCRIPT=1. Föredra create_model – den är säker och lättare att justera."""
    m = await _run(store.create, name, "script", {"code": code}, description)
    return _summary(m)


@mcp.tool()
async def update_model(model_id: str, changes: dict | None = None, remove: list[str] | None = None,
                       params: dict | None = None, note: str = "", name: str | None = None,
                       description: str | None = None, resolves: list[str] | None = None) -> dict:
    """Justerar en modell och skapar en ny version (webbvyn uppdateras direkt).

    changes: {"nyckel": värde} – för CSG-modeller sätter det vars[nyckel], annars parametern.
             {"/json/pekare": värde} – sätter valfritt värde i receptet, t.ex.
             {"/root/children/2/translate/0": 15} eller {"/root/children/-": {...}} (lägg till).
    remove:  lista med JSON-pekare att ta bort, t.ex. ["/root/children/3"].
    params:  ersätt hela receptet (spec för CSG, {"code": ...} för script).
    note:    kort beskrivning av ändringen (visas i historiken).
    resolves: kommentar-id:n (t.ex. ["c2"]) som ändringen åtgärdar – markeras lösta med svar.
    Läs receptet med get_model först om du är osäker på strukturen.
    """
    m = await _run(store.update, model_id, params=params, changes=changes, remove=remove,
                   note=note, name=name, description=description, resolves=resolves)
    return _summary(m)


@mcp.tool()
def get_model(model_id: str) -> dict:
    """Hämtar modellens nuvarande recept (params), mått per del, versionshistorik och öppna
    kommentarer. En kommentar har text, point (mm, i delens koordinater = STL-filens), part,
    version och near_nodes (JSON-pekare till de närmaste noderna i receptet)."""
    return _summary(store.get(model_id), full=True)


@mcp.tool()
def reply_to_comment(model_id: str, comment_id: str, text: str, resolve: bool = False) -> dict:
    """Svarar på en kommentar från webben (t.ex. en motfråga), och markerar den löst om resolve=True.
    Svaret syns direkt i 3D-vyn. Använd update_model(..., resolves=[id]) när du ändrat modellen."""
    m = store.comment_action(model_id, comment_id, "resolve" if resolve else "reply", text,
                             author="claude")
    c = Store._comment(m, comment_id)
    return {**_comment_view(c), "status": c["status"]}


@mcp.tool()
async def render_model(model_id: str, version: int | None = None,
                       views: list[str] | None = None) -> list:
    """Renderar modellen till en bild så att du kan granska den visuellt.
    views: välj bland iso, front, top, right, back_iso (default iso, front, top, right)."""
    m = store.get(model_id)
    v = Store.version(m, version) if version else Store.current(m)
    if v.get("pruned"):
        raise ValueError("versionens filer är rensade – använd revert_model först")
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
    """Listar alla modeller i biblioteket (senast ändrad först)."""
    out = []
    for m in store.list():
        cur = Store.current(m)
        out.append({"model_id": m["id"], "name": m["name"], "kind": cur["kind"],
                    "version": cur["version"], "parts": list(cur["parts"]),
                    "open_comments": _open(m), "viewer_url": f"{PUBLIC_URL}/m/{m['id']}"})
    return out


@mcp.tool()
async def revert_model(model_id: str, version: int) -> dict:
    """Återställer modellen till en tidigare version (skapas som en ny version)."""
    return _summary(await _run(store.revert, model_id, version))


@mcp.tool()
async def duplicate_model(model_id: str, name: str) -> dict:
    """Kopierar en modell till en ny (för att prova en variant utan att röra originalet)."""
    return _summary(await _run(store.duplicate, model_id, name))


@mcp.tool()
def delete_model(model_id: str) -> str:
    """Raderar en modell och hela dess historik."""
    store.delete(model_id)
    return f"raderade {model_id}"


# ---------------------------------------------------------------- fristående filer (Tinkercad m.fl.)

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
    """Platt fil ('lada.stl') eller en modells fil ('<model_id>/v2/botten.stl')."""
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
    parts: [{file: 'lada_botten.stl', color: 'red' | '#rrggbb' (valfri)}]. Modellbibliotekets
    delar anges som '<model_id>/<fil>' (fil enligt parts[...].url i get_model, t.ex. 'lada/v3/lock.stl').
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


# ---------------------------------------------------------------- auth

def _token_ok(request) -> bool:
    supplied = (request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                or request.query_params.get("token", "")
                or request.cookies.get("stl_token", ""))
    return bool(supplied) and hmac.compare_digest(supplied, TOKEN)


class Auth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not TOKEN:
            return await call_next(request)
        path, method = request.url.path, request.method
        ok = _token_ok(request)
        read = method in ("GET", "HEAD")
        public = (path == "/health" or path.startswith("/static/")
                  or (FILES_PUBLIC and read and path.startswith("/files/"))
                  or (UI_PUBLIC and read and (path == "/" or path.startswith("/m/")
                                              or path.startswith("/api/"))))
        if not ok and not public:
            if read and "text/html" in request.headers.get("accept", ""):
                return HTMLResponse((WEB / "login.html").read_text(), status_code=401)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if ok and read and request.query_params.get("token") and "/mcp" not in path:
            # inloggning via ?token= : sätt cookie och ta bort token ur adressen
            resp = RedirectResponse(path, status_code=303)
            resp.set_cookie("stl_token", TOKEN, max_age=60 * 60 * 24 * 90, httponly=True,
                            samesite="lax", secure=PUBLIC_URL.startswith("https"))
            return resp
        return await call_next(request)


# ---------------------------------------------------------------- webb

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
                               changes=body.get("changes"), note=body.get("note", "från webben"))
            elif action == "comment_add":
                await _run(store.add_comment, mid, body.get("text", ""), body.get("point"),
                           body.get("normal"), body.get("part"), body.get("version"))
                m = store.get(mid)
            elif action in ("comment_resolve", "comment_reopen", "comment_reply", "comment_delete"):
                m = store.comment_action(mid, body["id"], action.removeprefix("comment_"),
                                         body.get("text", ""))
            elif action == "duplicate":
                m = await _run(store.duplicate, mid, body.get("name") or f"{mid} kopia")
            else:
                return JSONResponse({"error": "okänd action"}, status_code=400)
            return JSONResponse({"ok": True, "id": m["id"]})
        m = store.get(mid)
    except Exception as e:  # noqa: BLE001 – felet visas i UI:t
        return _err(e)
    for v in m["versions"]:
        for p in v["parts"].values():
            p["url"] = f"/files/{m['id']}/{p['file']}"
    m["revision"] = store.revision
    return JSONResponse(m)


async def api_events(request):
    """Server-sent events: skickar {revision, id} när något i biblioteket ändras."""
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
    """PNG-förhandsvisning (samma rendering som Claude ser)."""
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
    routes=[
        Route("/", page_index),
        Route("/m/{id}", page_model),
        Route("/api/models", api_models),
        Route("/api/models/{id}", api_model, methods=["GET", "POST", "DELETE"]),
        Route("/api/models/{id}/preview.png", preview),
        Route("/api/events", api_events),
        Route("/files/{name}", flat_files),
        Route("/files/{id}/{rel:path}", files),
        Route("/upload/{name}", upload, methods=["PUT", "POST"]),
        Route("/health", health),
        Mount("/static", app=StaticFiles(directory=WEB)),
        Mount("/", app=mcp_app),
    ],
    lifespan=mcp_app.router.lifespan_context,
)
app.add_middleware(Auth)

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8765")))
