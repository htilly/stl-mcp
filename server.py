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
  HOST / PORT    default 0.0.0.0 / 8765
"""
import asyncio
import hmac
import json
import os
from pathlib import Path

import anyio
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

store = Store(DATA, allow_script=ALLOW_SCRIPT)

mcp = FastMCP(
    "stl-maker",
    instructions=(
        "Skapar och justerar 3D-modeller för utskrift (mm, z uppåt). Varje modell är ett recept "
        "med versionshistorik och visas live i ett webbgränssnitt (viewer_url) där användaren "
        "kan rotera modellen. Arbetsflöde: create_model (CSG-spec) eller create_enclosure → "
        "render_model för att själv se resultatet → update_model med små ändringar (t.ex. "
        "{'wall': 3} eller {'/root/children/1/d': 8}) tills användaren är nöjd. Använd "
        "variabler i 'vars' för mått som troligen kommer justeras. get_model visar receptet. "
        "Ge alltid användaren viewer_url."),
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
    return out


async def _run(fn, *a, **kw):
    """Kör tunga byggen i tråd så servern (och webb-UI:t) förblir responsiv."""
    return await anyio.to_thread.run_sync(lambda: fn(*a, **kw))


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
                       description: str | None = None) -> dict:
    """Justerar en modell och skapar en ny version (webbvyn uppdateras direkt).

    changes: {"nyckel": värde} – för CSG-modeller sätter det vars[nyckel], annars parametern.
             {"/json/pekare": värde} – sätter valfritt värde i receptet, t.ex.
             {"/root/children/2/translate/0": 15} eller {"/root/children/-": {...}} (lägg till).
    remove:  lista med JSON-pekare att ta bort, t.ex. ["/root/children/3"].
    params:  ersätt hela receptet (spec för CSG, {"code": ...} för script).
    note:    kort beskrivning av ändringen (visas i historiken).
    Läs receptet med get_model först om du är osäker på strukturen.
    """
    m = await _run(store.update, model_id, params=params, changes=changes, remove=remove,
                   note=note, name=name, description=description)
    return _summary(m)


@mcp.tool()
def get_model(model_id: str) -> dict:
    """Hämtar modellens nuvarande recept (params), mått per del och versionshistorik."""
    return _summary(store.get(model_id), full=True)


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
                    "viewer_url": f"{PUBLIC_URL}/m/{m['id']}"})
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
                    "versions": len(m["versions"]),
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
        Route("/files/{id}/{rel:path}", files),
        Route("/health", health),
        Mount("/static", app=StaticFiles(directory=WEB)),
        Mount("/", app=mcp_app),
    ],
    lifespan=mcp_app.router.lifespan_context,
)
app.add_middleware(Auth)

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8765")))
