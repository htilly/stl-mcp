"""Modellbibliotek på disk med versionshistorik.

En *modell* är ett recept (kind + params) – inte bara en STL. Varje ändring blir en ny version
som byggs om från receptet, så Claude kan justera stegvis och man kan backa.

Layout:
  <STL_DIR>/models/<id>/model.json
  <STL_DIR>/models/<id>/v<N>/<del>.stl
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import trimesh

import csg
import geometry as g

MAX_VERSIONS = 50  # äldre versioners STL-filer rensas (receptet behålls)


# ---------------------------------------------------------------- byggare per kind

def _build_csg(p):
    return csg.build(p)


def _build_enclosure(p):
    p = dict(p)
    body, lid = g.enclosure(
        p.pop("inner_x"), p.pop("inner_y"), p.pop("inner_z"),
        wall=p.pop("wall", 2.4), floor=p.pop("floor", 2.4), lid_t=p.pop("lid_thickness", 2.4),
        screws=p.pop("corner_screws", True), **p)
    return {"botten": body, "lock": lid}


def _build_primitive(p):
    p = dict(p)
    mesh = g.primitive(p.pop("shape"), **p)
    mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
    return {"model": mesh}


def _build_script(p, allow: bool):
    if not allow:
        raise PermissionError("script-modeller är avstängda (sätt ALLOW_SCRIPT=1 på servern)")
    code = p["code"]
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "s.py"
        script.write_text(
            "import sys, json, numpy as np, trimesh\n"
            f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
            "import geometry as g\n" + code +
            "\nif 'result' in dir():\n    parts = {'model': result}\n"
            "elif isinstance(parts, list):\n    parts = {'model': g.union(parts)}\n"
            f"for k, m in parts.items():\n    m.export({td!r} + '/out_' + k + '.stl')\n")
        r = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True,
                           timeout=120, cwd=td)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-1500:])
        return {f.stem[4:]: trimesh.load(f) for f in sorted(Path(td).glob("out_*.stl"))}


# ---------------------------------------------------------------- JSON-pekare

def _ptr_parts(ptr: str) -> list:
    if not ptr.startswith("/"):
        raise ValueError(f"JSON-pekare måste börja med '/': {ptr}")
    return [p.replace("~1", "/").replace("~0", "~") for p in ptr[1:].split("/")]


def _walk(doc, parts):
    cur = doc
    for p in parts:
        if isinstance(cur, list):
            cur = cur[int(p)]
        elif isinstance(cur, dict):
            if p not in cur:
                raise KeyError(f"saknas: '{p}'")
            cur = cur[p]
        else:
            raise KeyError(f"kan inte gå in i {type(cur).__name__} med '{p}'")
    return cur


def ptr_set(doc, ptr: str, value):
    parts = _ptr_parts(ptr)
    parent = _walk(doc, parts[:-1])
    key = parts[-1]
    if isinstance(parent, list):
        if key == "-":
            parent.append(value)
        else:
            parent[int(key)] = value
    elif isinstance(parent, dict):
        parent[key] = value
    else:
        raise KeyError(f"{ptr}: föräldern är inte objekt/lista")


def ptr_remove(doc, ptr: str):
    parts = _ptr_parts(ptr)
    parent = _walk(doc, parts[:-1])
    key = parts[-1]
    if isinstance(parent, list):
        parent.pop(int(key))
    else:
        del parent[key]


def apply_changes(kind: str, params: dict, changes: dict | None, remove: list[str] | None) -> dict:
    """changes: {"/json/pekare": värde} eller {"nyckel": värde}.
    Nyckel utan '/' : för csg -> vars[nyckel], annars -> params[nyckel]."""
    p = copy.deepcopy(params)
    for k, v in (changes or {}).items():
        if k.startswith("/"):
            ptr_set(p, k, v)
        elif kind == "csg":
            p.setdefault("vars", {})[k] = v
        else:
            p[k] = v
    for r in remove or []:
        ptr_remove(p, r)
    return p


# ---------------------------------------------------------------- store

def slug(name: str) -> str:
    s = name.lower()
    for a, b in (("å", "a"), ("ä", "a"), ("ö", "o"), ("é", "e"), ("ü", "u")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9_-]+", "-", s).strip("-_")
    return s[:50] or "modell"


def _stats(mesh: trimesh.Trimesh) -> dict:
    return {
        "watertight": bool(mesh.is_watertight),
        "extents_mm": [round(float(v), 2) for v in mesh.extents],
        "bounds_mm": [[round(float(v), 2) for v in row] for row in mesh.bounds],
        "volume_cm3": round(float(mesh.volume) / 1000, 3),
        "triangles": int(len(mesh.faces)),
    }


class Store:
    def __init__(self, root: Path, allow_script: bool = False):
        self.dir = Path(root) / "models"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.allow_script = allow_script
        self.lock = threading.RLock()
        self.revision = 0          # ökas vid varje ändring (webb-UI pollar via SSE)
        self.last_changed: str | None = None

    # -- intern
    def _path(self, mid: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", mid or ""):
            raise KeyError(f"ogiltigt modell-id: {mid}")
        return self.dir / mid

    def _read(self, mid: str) -> dict:
        f = self._path(mid) / "model.json"
        if not f.is_file():
            raise KeyError(f"modellen '{mid}' finns inte")
        return json.loads(f.read_text())

    def _write(self, m: dict):
        f = self._path(m["id"]) / "model.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1))
        tmp.replace(f)
        self.revision += 1
        self.last_changed = m["id"]

    def _bump(self, mid):
        self.revision += 1
        self.last_changed = mid

    def build(self, kind: str, params: dict) -> dict:
        if kind == "csg":
            return _build_csg(params)
        if kind == "enclosure":
            return _build_enclosure(params)
        if kind == "primitive":
            return _build_primitive(params)
        if kind == "script":
            return _build_script(params, self.allow_script)
        raise ValueError(f"okänd kind '{kind}'")

    def _new_version(self, m: dict, kind: str, params: dict, note: str) -> dict:
        t0 = time.time()
        parts = self.build(kind, params)          # kastar vid fel -> inget sparas
        if not parts:
            raise ValueError("bygget gav inga delar")
        ver = (m["versions"][-1]["version"] + 1) if m["versions"] else 1
        vdir = self._path(m["id"]) / f"v{ver}"
        vdir.mkdir(parents=True, exist_ok=True)
        pinfo = {}
        for pname, mesh in parts.items():
            fname = f"{slug(pname)}.stl"
            mesh.export(vdir / fname)
            pinfo[pname] = {"file": f"v{ver}/{fname}", **_stats(mesh)}
        v = {"version": ver, "kind": kind, "params": params, "note": note or "",
             "created": time.time(), "build_seconds": round(time.time() - t0, 2), "parts": pinfo}
        m["versions"].append(v)
        m["current"] = ver
        m["updated"] = v["created"]
        # rensa gamla STL:er (receptet finns kvar så de kan återskapas med revert)
        for old in m["versions"][:-MAX_VERSIONS]:
            if not old.get("pruned"):
                shutil.rmtree(self._path(m["id"]) / f"v{old['version']}", ignore_errors=True)
                old["pruned"] = True
        return v

    # -- publikt
    def create(self, name: str, kind: str, params: dict, description: str = "",
               note: str = "") -> dict:
        with self.lock:
            base = slug(name)
            mid, i = base, 2
            while (self.dir / mid).exists():
                mid, i = f"{base}-{i}", i + 1
            self._path(mid).mkdir(parents=True)
            m = {"id": mid, "name": name, "description": description, "created": time.time(),
                 "updated": time.time(), "current": 0, "versions": []}
            try:
                self._new_version(m, kind, params, note or "skapad")
            except Exception:
                shutil.rmtree(self._path(mid), ignore_errors=True)
                raise
            self._write(m)
            return m

    def update(self, mid: str, *, params: dict | None = None, changes: dict | None = None,
               remove: list[str] | None = None, note: str = "", name: str | None = None,
               description: str | None = None) -> dict:
        with self.lock:
            m = self._read(mid)
            cur = self.current(m)
            kind = cur["kind"]
            if params is not None or changes or remove:
                base = params if params is not None else cur["params"]
                newp = apply_changes(kind, base, changes, remove)
                if not note:
                    bits = [f"{k}={json.dumps(v, ensure_ascii=False)[:30]}" for k, v in (changes or {}).items()]
                    bits += [f"-{r}" for r in remove or []]
                    note = ", ".join(bits) or "nytt recept"
                self._new_version(m, kind, newp, note)
            if name is not None:
                m["name"] = name
            if description is not None:
                m["description"] = description
            m["updated"] = time.time()
            self._write(m)
            return m

    def revert(self, mid: str, version: int) -> dict:
        with self.lock:
            m = self._read(mid)
            src = self.version(m, version)
            self._new_version(m, src["kind"], copy.deepcopy(src["params"]),
                              f"återställd från v{version}")
            self._write(m)
            return m

    def set_current(self, mid: str, version: int) -> dict:
        """Peka om 'current' utan att skapa ny version (används av webb-UI:t)."""
        with self.lock:
            m = self._read(mid)
            v = self.version(m, version)
            if v.get("pruned"):
                raise ValueError("versionens filer är rensade – använd revert")
            m["current"] = version
            self._write(m)
            return m

    def duplicate(self, mid: str, name: str) -> dict:
        m = self._read(mid)
        cur = self.current(m)
        return self.create(name, cur["kind"], copy.deepcopy(cur["params"]), m.get("description", ""),
                           f"kopia av {mid} v{cur['version']}")

    def delete(self, mid: str):
        with self.lock:
            p = self._path(mid)
            if not p.is_dir():
                raise KeyError(f"modellen '{mid}' finns inte")
            shutil.rmtree(p)
            self._bump(mid)

    def get(self, mid: str) -> dict:
        return self._read(mid)

    def list(self) -> list[dict]:
        out = []
        for f in self.dir.glob("*/model.json"):
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(out, key=lambda m: m.get("updated", 0), reverse=True)

    def file(self, mid: str, rel: str) -> Path:
        p = (self._path(mid) / rel).resolve()
        if self._path(mid).resolve() not in p.parents or p.suffix != ".stl" or not p.is_file():
            raise KeyError("filen finns inte")
        return p

    @staticmethod
    def current(m: dict) -> dict:
        return Store.version(m, m["current"])

    @staticmethod
    def version(m: dict, version: int) -> dict:
        for v in m["versions"]:
            if v["version"] == version:
                return v
        raise KeyError(f"version {version} finns inte för '{m['id']}'")
