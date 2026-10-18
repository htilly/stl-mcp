"""On-disk model library with version history.

A *model* is a recipe (kind + params) – not just an STL. Every change becomes a new version
that is rebuilt from the recipe, so Claude can adjust step by step and you can roll back.

Layout:
  <STL_DIR>/models/<id>/model.json
  <STL_DIR>/models/<id>/v<N>/<part>.stl
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
import printcheck

MAX_VERSIONS = 50  # older versions' STL files are pruned (the recipe is kept)


# ---------------------------------------------------------------- builders per kind

def _build_csg(p):
    return csg.build(p)


def _build_enclosure(p):
    p = dict(p)
    body, lid = g.enclosure(
        p.pop("inner_x"), p.pop("inner_y"), p.pop("inner_z"),
        wall=p.pop("wall", 2.4), floor=p.pop("floor", 2.4), lid_t=p.pop("lid_thickness", 2.4),
        screws=p.pop("corner_screws", True), **p)
    return {"body": body, "lid": lid}


def _build_primitive(p):
    p = dict(p)
    mesh = g.primitive(p.pop("shape"), **p)
    mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
    return {"model": mesh}


def _build_script(p, allow: bool):
    if not allow:
        raise PermissionError("script models are disabled (set ALLOW_SCRIPT=1 on the server)")
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


# ---------------------------------------------------------------- JSON pointers

def _ptr_parts(ptr: str) -> list:
    if not ptr.startswith("/"):
        raise ValueError(f"JSON pointer must start with '/': {ptr}")
    return [p.replace("~1", "/").replace("~0", "~") for p in ptr[1:].split("/")]


def _walk(doc, parts):
    cur = doc
    for p in parts:
        if isinstance(cur, list):
            cur = cur[int(p)]
        elif isinstance(cur, dict):
            if p not in cur:
                raise KeyError(f"missing: '{p}'")
            cur = cur[p]
        else:
            raise KeyError(f"cannot descend into {type(cur).__name__} with '{p}'")
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
        raise KeyError(f"{ptr}: parent is not an object/list")


def ptr_remove(doc, ptr: str):
    parts = _ptr_parts(ptr)
    parent = _walk(doc, parts[:-1])
    key = parts[-1]
    if isinstance(parent, list):
        parent.pop(int(key))
    else:
        del parent[key]


def apply_changes(kind: str, params: dict, changes: dict | None, remove: list[str] | None) -> dict:
    """changes: {"/json/pointer": value} or {"key": value}.
    A key without '/': for csg -> vars[key], otherwise -> params[key]."""
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
    return s[:50] or "model"


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
        self.settings = printcheck.Settings(Path(root) / "settings.json")
        self.lock = threading.RLock()
        self.revision = 0          # incremented on every change (the web UI polls via SSE)
        self.last_changed: str | None = None

    # -- intern
    def _path(self, mid: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", mid or ""):
            raise KeyError(f"invalid model id: {mid}")
        return self.dir / mid

    def _read(self, mid: str) -> dict:
        f = self._path(mid) / "model.json"
        if not f.is_file():
            raise KeyError(f"model '{mid}' does not exist")
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
        raise ValueError(f"unknown kind '{kind}'")

    def _new_version(self, m: dict, kind: str, params: dict, note: str) -> dict:
        t0 = time.time()
        parts = self.build(kind, params)          # raises on error -> nothing is saved
        if not parts:
            raise ValueError("the build produced no parts")
        ver = (m["versions"][-1]["version"] + 1) if m["versions"] else 1
        vdir = self._path(m["id"]) / f"v{ver}"
        vdir.mkdir(parents=True, exist_ok=True)
        pinfo = {}
        for pname, mesh in parts.items():
            fname = f"{slug(pname)}.stl"
            mesh.export(vdir / fname)
            pinfo[pname] = {"file": f"v{ver}/{fname}", **_stats(mesh),
                            "warnings": printcheck.check(mesh, self.settings.get())}
        v = {"version": ver, "kind": kind, "params": params, "note": note or "",
             "created": time.time(), "build_seconds": round(time.time() - t0, 2), "parts": pinfo}
        m["versions"].append(v)
        m["current"] = ver
        m["updated"] = v["created"]
        # prune old STLs (the recipe remains so they can be recreated with revert)
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
                self._new_version(m, kind, params, note or "created")
            except Exception:
                shutil.rmtree(self._path(mid), ignore_errors=True)
                raise
            self._write(m)
            return m

    def update(self, mid: str, *, params: dict | None = None, changes: dict | None = None,
               remove: list[str] | None = None, note: str = "", name: str | None = None,
               description: str | None = None, resolves: list[str] | None = None) -> dict:
        with self.lock:
            m = self._read(mid)
            cur = self.current(m)
            kind = cur["kind"]
            for cid in resolves or []:
                self._comment(m, cid)                          # validate before the build
            if params is not None or changes or remove:
                base = params if params is not None else cur["params"]
                newp = apply_changes(kind, base, changes, remove)
                if not note:
                    bits = [f"{k}={json.dumps(v, ensure_ascii=False)[:30]}" for k, v in (changes or {}).items()]
                    bits += [f"-{r}" for r in remove or []]
                    note = ", ".join(bits) or "new recipe"
                self._new_version(m, kind, newp, note)
            for cid in resolves or []:
                c = self._comment(m, cid)
                c["status"] = "resolved"
                msg = f"Addressed in v{m['current']}" + (f": {note}" if note else "")
                c["replies"].append({"author": "claude", "text": msg, "at": time.time(),
                                     "version": m["current"]})
            if name is not None:
                m["name"] = name
            if description is not None:
                m["description"] = description
            m["updated"] = time.time()
            self._write(m)
            return m

    def recheck(self, mid: str | None = None) -> int:
        """Re-run the print checks on the current version of one model (or all) with the current
        settings, without creating a version. Returns the number of models updated."""
        n = 0
        with self.lock:
            for m in ([self._read(mid)] if mid else self.list()):
                cur = self.current(m)
                if cur.get("pruned"):
                    continue
                settings = self.settings.get()
                for p in cur["parts"].values():
                    path = self._path(m["id"]) / p["file"]
                    if path.is_file():
                        p["warnings"] = printcheck.check(trimesh.load(path, force="mesh"), settings)
                self._write(m)
                n += 1
        return n

    def revert(self, mid: str, version: int) -> dict:
        with self.lock:
            m = self._read(mid)
            src = self.version(m, version)
            self._new_version(m, src["kind"], copy.deepcopy(src["params"]),
                              f"reverted from v{version}")
            self._write(m)
            return m

    def set_current(self, mid: str, version: int) -> dict:
        """Repoint 'current' without creating a new version (used by the web UI)."""
        with self.lock:
            m = self._read(mid)
            v = self.version(m, version)
            if v.get("pruned"):
                raise ValueError("the version's files have been pruned – use revert")
            m["current"] = version
            self._write(m)
            return m

    def duplicate(self, mid: str, name: str) -> dict:
        m = self._read(mid)
        cur = self.current(m)
        return self.create(name, cur["kind"], copy.deepcopy(cur["params"]), m.get("description", ""),
                           f"copy of {mid} v{cur['version']}")

    # -- comments (pointed out in the web 3D view, read and answered by Claude)
    @staticmethod
    def _comment(m: dict, cid: str) -> dict:
        for c in m.get("comments", []):
            if c["id"] == cid:
                return c
        raise KeyError(f"comment '{cid}' does not exist on '{m['id']}'")

    def _hints(self, m: dict, version: int, part: str | None, point) -> dict:
        """Link a point to the recipe: nearest CSG nodes, or inner coordinates for enclosures."""
        v = self.version(m, version)
        try:
            if v["kind"] == "csg":
                return {"near_nodes": csg.locate(v["params"], part, point)}
            if v["kind"] == "enclosure":
                p = v["params"]
                w, fl = p.get("wall", 2.4), p.get("floor", 2.4)
                inner = [round(point[0] - w, 2), round(point[1] - w, 2)]
                if part == "body":
                    inner.append(round(point[2] - fl, 2))
                return {"inner_xy" if part != "body" else "inner_xyz": inner}
        except Exception:  # noqa: BLE001 – hints are best effort
            pass
        return {}

    def add_comment(self, mid: str, text: str, point=None, normal=None, part: str | None = None,
                    version: int | None = None, author: str = "user") -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("the comment is empty")
        with self.lock:
            m = self._read(mid)
            comments = m.setdefault("comments", [])
            ver = int(version or m["current"])
            self.version(m, ver)
            n = max([int(c["id"][1:]) for c in comments] or [0]) + 1
            c = {"id": f"c{n}", "text": text[:2000], "author": author, "created": time.time(),
                 "version": ver, "part": part, "status": "open", "replies": []}
            if point is not None:
                c["point"] = [round(float(x), 2) for x in point][:3]
                if normal is not None:
                    c["normal"] = [round(float(x), 3) for x in normal][:3]
                c.update(self._hints(m, ver, part, c["point"]))
            comments.append(c)
            self._write(m)
            return c

    def comment_action(self, mid: str, cid: str, action: str, text: str = "",
                       author: str = "user") -> dict:
        with self.lock:
            m = self._read(mid)
            if action == "delete":
                self._comment(m, cid)
                m["comments"] = [c for c in m["comments"] if c["id"] != cid]
                self._write(m)
                return m
            c = self._comment(m, cid)
            if action in ("reply", "resolve") and text.strip():
                c["replies"].append({"author": author, "text": text.strip()[:2000], "at": time.time(),
                                     "version": m["current"]})
            if action == "resolve":
                c["status"] = "resolved"
            elif action == "reopen":
                c["status"] = "open"
            elif action != "reply":
                raise ValueError(f"unknown comment action '{action}'")
            self._write(m)
            return m

    def delete(self, mid: str):
        with self.lock:
            p = self._path(mid)
            if not p.is_dir():
                raise KeyError(f"model '{mid}' does not exist")
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
            raise KeyError("the file does not exist")
        return p

    @staticmethod
    def current(m: dict) -> dict:
        return Store.version(m, m["current"])

    @staticmethod
    def version(m: dict, version: int) -> dict:
        for v in m["versions"]:
            if v["version"] == version:
                return v
        raise KeyError(f"version {version} does not exist for '{m['id']}'")
