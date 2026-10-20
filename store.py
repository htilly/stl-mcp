"""Model library with version history.

A *model* is a recipe (kind + params) – not just an STL. Every change becomes a new version
that is rebuilt from the recipe, so Claude can adjust step by step and you can roll back.

Each model has an owner. The model document (recipe, versions, comments) lives in Postgres
(table models, see migrations/002_models.sql); the built files stay on disk:
  <STL_DIR>/models/<id>/v<N>/<part>.stl

Store itself is unrestricted; the server always goes through Store.view(user), which only lets a
user reach their own models (admins reach all). Someone else's model looks exactly like one that
does not exist - unless its owner made it public: then anyone can read it (not its comments) and
copy it, but only the owner (and admins) can change it.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import trimesh
from psycopg.types.json import Jsonb

import buildworker
import jobs
import csg
import openscad
import printcheck
import sandbox

MAX_VERSIONS = 50  # older versions' STL files are pruned (the recipe is kept)
MAX_COMMENTS = 200  # per model, open and resolved
# Each model is one row, rewritten on every change: keep what a user can put in it bounded.
MAX_RECIPE_BYTES = 2_000_000   # one version's params, as JSON
MAX_NAME = 200
MAX_DESCRIPTION = 5000
MAX_NOTE = 500


def check_sizes(name: str | None = None, description: str | None = None, note: str | None = None):
    for what, value, limit in (("name", name, MAX_NAME), ("description", description, MAX_DESCRIPTION),
                               ("note", note, MAX_NOTE)):
        if value is not None and len(value) > limit:
            raise ValueError(f"the {what} is longer than {limit} characters")


def check_recipe_size(params) -> None:
    n = len(json.dumps(params, ensure_ascii=False, default=str))
    if n > MAX_RECIPE_BYTES:
        raise ValueError(f"the recipe is {n // 1000} kB - at most {MAX_RECIPE_BYTES // 1000} kB")


# ---------------------------------------------------------------- builders per kind

def _build_script(p, allow: bool):
    """Without the sandbox: arbitrary Python in a plain subprocess of the server (ALLOW_SCRIPT)."""
    if sandbox.available():
        return sandbox.build("script", p)
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
        # A bare environment: the script must not see DATABASE_URL, MCP_TOKEN or SSO secrets.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": td, "TMPDIR": td,
               "LANG": "C.UTF-8"}
        p = jobs.Popen([sys.executable, "-I", str(script)], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, cwd=td, env=env)
        jobs.add_pid(p.pid)
        try:
            _, err = p.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise
        finally:
            jobs.remove_pid(p.pid)
        if p.returncode != 0:
            raise RuntimeError(err[-1500:])
        return {f.stem[4:]: trimesh.load(f) for f in sorted(Path(td).glob("out_*.stl"))}


def _build_openscad(p, allow: bool, mesh_dir: Path | None = None):
    if not allow:
        raise PermissionError("OpenSCAD models are disabled on this server (ALLOW_OPENSCAD=0 "
                              "or the openscad binary is missing)")
    return openscad.build(p, mesh_dir)


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
    A key without '/': for csg, openscad and build123d -> vars[key] ("code"/"parts" for openscad
    and "code"/"tolerance" for build123d replace those), otherwise -> params[key]."""
    p = copy.deepcopy(params)
    for k, v in (changes or {}).items():
        if k.startswith("/"):
            ptr_set(p, k, v)
        elif kind == "csg" or (kind == "openscad" and k not in ("code", "parts", "vars")) \
                or (kind == "build123d" and k not in ("code", "vars", "tolerance", "angular_tolerance")):
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


def _missing(mid: str) -> KeyError:
    return KeyError(f"model '{mid}' does not exist")


VISIBILITIES = ("private", "public")


def _doc(row: dict) -> dict:
    """A model document with its visibility from the column (the column is what queries use)."""
    return {**row["doc"], "visibility": row["visibility"]}


class Store:
    def __init__(self, root: Path, db, allow_script: bool = False, allow_openscad: bool = False):
        self.dir = Path(root) / "models"
        self.dir.mkdir(parents=True, exist_ok=True)
        # meshes attached to recipes (OpenSCAD's import(), csg's "mesh" shape), by content:
        # <sha256>.stl (see openscad.py)
        self.mesh_dir = Path(root) / "meshes"
        self.mesh_dir.mkdir(exist_ok=True)
        csg.MESH_DIR = self.mesh_dir      # for csg.locate, which builds nodes in this process
        self.db = db
        self.allow_script = allow_script
        self.allow_openscad = allow_openscad
        self.settings = printcheck.Settings(Path(root) / "settings.json")
        self.lock = threading.RLock()
        self.revision = 0          # incremented on every change (the web UI polls via SSE)
        self.last_changed: str | None = None
        # per owner: the revision of their last change and which model it was, so each user's
        # live updates only fire for their own library
        self.owner_revision: dict[str | None, int] = {}
        self.owner_last_changed: dict[str | None, str] = {}

    # -- intern
    def _path(self, mid: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_-]{1,60}", mid or ""):
            raise KeyError(f"invalid model id: {mid}")
        return self.dir / mid

    def _read(self, mid: str) -> dict:
        self._path(mid)  # validates the id
        row = self.db.one("SELECT doc, visibility FROM models WHERE id = %s", (mid,))
        if not row:
            raise _missing(mid)
        return _doc(row)

    def _write(self, m: dict):
        self.db.execute(
            "INSERT INTO models (id, owner_id, name, visibility, updated, doc) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET owner_id = EXCLUDED.owner_id, name = EXCLUDED.name, "
            "visibility = EXCLUDED.visibility, updated = EXCLUDED.updated, doc = EXCLUDED.doc",
            (m["id"], m.get("owner"), m["name"], m.get("visibility", "private"),
             m.get("updated", time.time()), Jsonb(m)))
        self._bump(m["id"], m.get("owner"))

    def _bump(self, mid, owner):
        self.revision += 1
        self.last_changed = mid
        self.owner_revision[owner] = self.revision
        self.owner_last_changed[owner] = mid

    def owner(self, mid: str) -> str | None:
        """The model's owner id; KeyError if the model does not exist."""
        return self.access(mid)[0]

    def access(self, mid: str) -> tuple[str | None, str]:
        """(owner id, visibility) of a model; KeyError if it does not exist."""
        self._path(mid)
        row = self.db.one("SELECT owner_id, visibility FROM models WHERE id = %s", (mid,))
        if not row:
            raise _missing(mid)
        return row["owner_id"], row["visibility"]

    def view(self, user: dict) -> "StoreView":
        return StoreView(self, user)

    def build(self, kind: str, params: dict) -> dict:
        if kind in buildworker.KINDS:      # in a sandboxed worker process, see buildworker.py
            return buildworker.build(kind, params, mesh_dir=self.mesh_dir)
        if kind == "script":
            return _build_script(params, self.allow_script)
        if kind == "openscad":
            return _build_openscad(params, self.allow_openscad, self.mesh_dir)
        if kind == "build123d":                # in the sandbox container, see sandboxd.py
            return sandbox.build(kind, params)
        raise ValueError(f"unknown kind '{kind}'")

    def _built(self, kind: str, params: dict, scripts: bool = True) -> tuple[dict, float]:
        """Builds outside self.lock, so a slow build does not hold up everyone else's changes.
        scripts=False refuses script models: they run Python on the server, so only admins may
        build them - checked here, where the code runs, whichever way the recipe got there
        (a copy of a public script model, an update or revert that keeps the kind)."""
        if kind == "script" and not scripts:
            raise PermissionError("script models are for admins only")
        check_recipe_size(params)
        t0 = time.time()
        return self.build(kind, params), time.time() - t0

    def _new_version(self, m: dict, kind: str, params: dict, note: str,
                     built: tuple[dict, float] | None = None, scripts: bool = True) -> dict:
        t0 = time.time()
        if built is None:
            built = self._built(kind, params, scripts)  # raises on error -> nothing is saved
        parts, seconds = built
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
             "created": time.time(), "build_seconds": round(seconds + time.time() - t0, 2),
             "parts": pinfo}
        for fname, data in getattr(parts, "files", {}).items():   # e.g. build123d's STEP export
            (vdir / fname).write_bytes(data)
            v.setdefault("files", {})[Path(fname).suffix[1:]] = f"v{ver}/{fname}"
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
               note: str = "", owner: str | None = None, scripts: bool = True) -> dict:
        check_sizes(name, description, note)
        built = self._built(kind, params, scripts)
        with self.lock:
            base = slug(name)
            mid, i = base, 2
            while (self.dir / mid).exists() or self.db.one("SELECT 1 FROM models WHERE id = %s", (mid,)):
                mid, i = f"{base}-{i}", i + 1
            self._path(mid).mkdir(parents=True)
            m = {"id": mid, "owner": owner, "name": name, "description": description,
                 "created": time.time(), "updated": time.time(), "current": 0, "versions": []}
            try:
                self._new_version(m, kind, params, note or "created", built)
            except Exception:
                shutil.rmtree(self._path(mid), ignore_errors=True)
                raise
            self._write(m)
            return m

    def update(self, mid: str, *, params: dict | None = None, changes: dict | None = None,
               remove: list[str] | None = None, note: str = "", name: str | None = None,
               description: str | None = None, resolves: list[str] | None = None,
               kind: str | None = None, scripts: bool = True) -> dict:
        """kind rebuilds the model with another builder (e.g. enclosure -> openscad) as a new
        version of the same model; it needs params, since the old recipe does not fit the new kind."""
        rebuild = params is not None or changes or remove
        check_sizes(name, description, note)

        def recipe(m):
            cur = self.current(m)
            for cid in resolves or []:
                self._comment(m, cid)                          # validate before the build
            if kind and kind != cur["kind"] and params is None:
                raise ValueError(f"changing kind from '{cur['kind']}' to '{kind}' needs params "
                                 "(the whole new recipe)")
            base = params if params is not None else cur["params"]
            k = kind or cur["kind"]
            return k, apply_changes(k, base, changes, remove)

        def head(m):
            return m["current"], len(m["versions"]), m["versions"][-1]["version"]

        if rebuild:                                            # build outside the lock ...
            m = self._read(mid)
            before, (kind, newp) = head(m), recipe(m)
            built = self._built(kind, newp, scripts)
        with self.lock:
            m = self._read(mid)
            for cid in resolves or []:
                self._comment(m, cid)
            if rebuild:
                if head(m) != before:                         # ... unless it changed meanwhile
                    kind, newp = recipe(m)
                    built = None
                if not note:
                    bits = [f"{k}={json.dumps(v, ensure_ascii=False)[:30]}" for k, v in (changes or {}).items()]
                    bits += [f"-{r}" for r in remove or []]
                    note = ", ".join(bits) or "new recipe"
                self._new_version(m, kind, newp, note, built, scripts)
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

    def revert(self, mid: str, version: int, scripts: bool = True) -> dict:
        src = self.version(self._read(mid), version)          # versions never change
        built = self._built(src["kind"], src["params"], scripts)
        with self.lock:
            m = self._read(mid)
            src = self.version(m, version)
            self._new_version(m, src["kind"], copy.deepcopy(src["params"]),
                              f"reverted from v{version}", built)
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

    def duplicate(self, mid: str, name: str, owner: str | None = None, scripts: bool = True) -> dict:
        """Copies the current recipe into a new, private model owned by owner (default: the same
        owner). A copy of someone else's model records where it came from (copied_from)."""
        m = self._read(mid)
        cur = self.current(m)
        owner = owner if owner is not None else m.get("owner")
        new = self.create(name, cur["kind"], copy.deepcopy(cur["params"]), m.get("description", ""),
                          f"copy of {mid} v{cur['version']}", owner=owner, scripts=scripts)
        if owner != m.get("owner"):
            with self.lock:
                new = self._read(new["id"])
                new["copied_from"] = {"model_id": mid, "owner_id": m.get("owner"),
                                      "name": m["name"], "version": cur["version"]}
                self._write(new)
        return new

    def set_visibility(self, mid: str, visibility: str) -> dict:
        """Makes a model private (only its owner and admins) or public (readable by everyone,
        listed in the gallery). Its versions and updated time stay as they are."""
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}")
        with self.lock:
            m = self._read(mid)
            if m["visibility"] != visibility:
                m["visibility"] = visibility
                m["published"] = time.time() if visibility == "public" else None
                self._write(m)
            return m

    def public(self, limit: int = 500) -> list[dict]:
        """Public models for the gallery, most recently changed first, each with owner_name and
        copies (how many models were copied from it)."""
        rows = self.db.all(
            "SELECT m.doc, m.visibility, u.display_name AS owner_name, "
            "(SELECT count(*) FROM models c WHERE c.doc->'copied_from'->>'model_id' = m.id) AS copies "
            "FROM models m LEFT JOIN users u ON u.id = m.owner_id "
            "WHERE m.visibility = 'public' ORDER BY m.updated DESC, m.id LIMIT %s", (limit,))
        return [{**_doc(r), "owner_name": r["owner_name"] or "", "copies": r["copies"]} for r in rows]

    def owner_name(self, owner: str | None) -> str:
        row = self.db.one("SELECT display_name FROM users WHERE id = %s", (owner,)) if owner else None
        return (row or {}).get("display_name") or ""

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
        m = self._read(mid)
        ver = int(version or m["current"])
        self.version(m, ver)
        c = {"text": text[:2000], "author": author, "created": time.time(),
             "version": ver, "part": part, "status": "open", "replies": []}
        if point is not None:
            c["point"] = [round(float(x), 2) for x in point][:3]
            if normal is not None:
                c["normal"] = [round(float(x), 3) for x in normal][:3]
            # Outside self.lock: locating the nearest CSG nodes builds parts of the recipe, which
            # can take a while for a heavy model. Versions never change, so the hints stay valid.
            c.update(self._hints(m, ver, part, c["point"]))
        with self.lock:
            m = self._read(mid)
            comments = m.setdefault("comments", [])
            if len(comments) >= MAX_COMMENTS:
                raise ValueError(f"the model has {MAX_COMMENTS} comments - delete some first")
            n = max([int(x["id"][1:]) for x in comments] or [0]) + 1
            c = {"id": f"c{n}", **c}
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
            row = self.db.one("DELETE FROM models WHERE id = %s RETURNING owner_id", (mid,))
            if not row:
                raise _missing(mid)
            shutil.rmtree(p, ignore_errors=True)
            self._bump(mid, row["owner_id"])

    def get(self, mid: str) -> dict:
        return self._read(mid)

    def list(self, owner: str | None = None, everyone: bool = True) -> list[dict]:
        """All models (everyone=True) or only owner's, most recently changed first."""
        if everyone:
            rows = self.db.all("SELECT doc, visibility FROM models ORDER BY updated DESC, id")
        else:
            rows = self.db.all("SELECT doc, visibility FROM models WHERE owner_id = %s "
                               "ORDER BY updated DESC, id", (owner,))
        return [_doc(r) for r in rows]

    def delete_owner(self, owner: str) -> int:
        """Deletes all of a user's models and their files. Returns how many."""
        with self.lock:
            rows = self.db.all("DELETE FROM models WHERE owner_id = %s RETURNING id", (owner,))
            for r in rows:
                shutil.rmtree(self._path(r["id"]), ignore_errors=True)
                self._bump(r["id"], owner)
            return len(rows)

    def import_legacy(self, owner: str) -> int:
        """Moves model.json files from before the database into it, owned by owner. Each file is
        renamed to model.json.imported, so this runs once per model. Returns how many."""
        n = 0
        with self.lock:
            for f in sorted(self.dir.glob("*/model.json")):
                try:
                    m = json.loads(f.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                m["owner"] = owner
                self.db.execute(
                    "INSERT INTO models (id, owner_id, name, updated, doc) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (m["id"], owner, m.get("name") or m["id"], m.get("updated") or time.time(), Jsonb(m)))
                f.replace(f.with_name("model.json.imported"))
                n += 1
        return n

    def revision_for(self, user: dict | None) -> int:
        """The revision a user's live view follows: everything for admins, else their own."""
        if user and user["role"] == "admin":
            return self.revision
        return self.owner_revision.get(user["id"] if user else None, 0)

    def last_changed_for(self, user: dict | None) -> str | None:
        if user and user["role"] == "admin":
            return self.last_changed
        return self.owner_last_changed.get(user["id"] if user else None)

    def file(self, mid: str, rel: str) -> Path:
        p = (self._path(mid) / rel).resolve()
        if self._path(mid).resolve() not in p.parents or p.suffix not in (".stl", ".step") \
                or not p.is_file():
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


class StoreView:
    """The library as one user sees it: their own models (admins: every model), plus read access
    to public models (reading, files, previews and copying - not changing them, not their
    comments). Everything else raises the same KeyError as a model that does not exist."""

    def __init__(self, store: Store, user: dict):
        self.store, self.user = store, user
        self.admin = user["role"] == "admin"

    def _check(self, mid: str, read: bool = False) -> str:
        owner, visibility = self.store.access(mid)
        if not (self.admin or owner == self.user["id"] or (read and visibility == "public")):
            raise _missing(mid)
        return mid

    def can_edit(self, m: dict) -> bool:
        return self.admin or m.get("owner") == self.user["id"]

    def create(self, name: str, kind: str, params: dict, description: str = "", note: str = "") -> dict:
        return self.store.create(name, kind, params, description, note, owner=self.user["id"],
                                 scripts=self.admin)

    def get(self, mid: str) -> dict:
        m = self.store.get(self._check(mid, read=True))
        if not self.can_edit(m):
            m = {**m, "comments": []}  # comments are between the owner and their Claude
        return m

    def set_visibility(self, mid: str, visibility: str) -> dict:
        return self.store.set_visibility(self._check(mid), visibility)

    def list(self) -> list[dict]:
        return self.store.list(self.user["id"], everyone=self.admin)

    def update(self, mid: str, **kw) -> dict:
        return self.store.update(self._check(mid), **kw, scripts=self.admin)

    def revert(self, mid: str, version: int) -> dict:
        return self.store.revert(self._check(mid), version, scripts=self.admin)

    def set_current(self, mid: str, version: int) -> dict:
        return self.store.set_current(self._check(mid), version)

    def duplicate(self, mid: str, name: str) -> dict:
        return self.store.duplicate(self._check(mid, read=True), name, owner=self.user["id"],
                                    scripts=self.admin)

    def add_comment(self, mid: str, text: str, *a, **kw) -> dict:
        return self.store.add_comment(self._check(mid), text, *a, **kw)

    def comment_action(self, mid: str, cid: str, action: str, text: str = "", author: str = "user") -> dict:
        return self.store.comment_action(self._check(mid), cid, action, text, author=author)

    def delete(self, mid: str):
        self.store.delete(self._check(mid))

    def recheck(self, mid: str | None = None) -> int:
        if mid is None and not self.admin:
            return sum(self.store.recheck(m["id"]) for m in self.list())
        return self.store.recheck(self._check(mid) if mid else None)

    def file(self, mid: str, rel: str) -> Path:
        return self.store.file(self._check(mid, read=True), rel)
