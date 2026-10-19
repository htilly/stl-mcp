"""One Python build (model kinds "build123d" and "script") inside the sandbox, see sandboxd.py.

Started by sandboxd as the slot's own user, it applies its rlimits, does its imports and waits
for one job on stdin: {"kind", "params", "limits"}. It runs the code and writes the reply to
the stdout it was started with (the code's own print() output goes to stderr):

    4-byte big-endian length, JSON header, then the payloads in header order:
    {"parts": [{"name", "nv", "nf"}], "files": [{"name", "size"}]}
    -> per part nv*3 float64 vertices and nf*3 int32 faces, then each file's bytes
    or {"error": type name, "message": str, "trace": the user code's part of the traceback}

Raw arrays, not pickles, so the server reads exactly numbers and nothing that runs code.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import struct
import sys
import tempfile
import traceback
from pathlib import Path

CODE_NAME = "<model>"        # the user code's file name in tracebacks
MAX_CODE = 200_000           # characters
MAX_TRIANGLES = 3_000_000    # over all parts


def _apply_limits(lim: dict):
    if sys.platform != "linux":
        return
    import resource
    mem = lim["mem_mb"] * 1024 * 1024
    for res, soft, hard in ((resource.RLIMIT_AS, mem, mem),
                            # SIGXCPU at the soft limit, SIGKILL a little later
                            (resource.RLIMIT_CPU, lim["cpu"], lim["cpu"] + 5),
                            (resource.RLIMIT_FSIZE, lim["fsize"], lim["fsize"]),
                            (resource.RLIMIT_NOFILE, 256, 256),
                            (resource.RLIMIT_CORE, 0, 0)):
        resource.setrlimit(res, (soft, hard))
    if lim.get("nproc"):     # per user: only meaningful when every slot has its own uid
        resource.setrlimit(resource.RLIMIT_NPROC, (lim["nproc"], lim["nproc"]))


def _workdir() -> Path:
    """A private temp dir. Leftovers of an earlier job of this slot (killed before it could
    clean up) belong to the same user, so this one can remove them."""
    tmp = Path(tempfile.gettempdir())
    prefix = f"sbx-{os.getuid()}-{os.environ.get('SANDBOX_SLOT', '0')}-"   # (one uid per slot as root)
    for old in tmp.glob(prefix + "*"):
        shutil.rmtree(old, ignore_errors=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=tmp))


# ---------------------------------------------------------------- running the code

def _with_vars(code: str, vars_: dict) -> ast.Module:
    """The code with top-level `name = ...` replaced by the override in vars (like openscad -D),
    so dimensions can be changed without rewriting the code."""
    if len(code) > MAX_CODE:
        raise ValueError(f"the code is too long (max {MAX_CODE} characters)")
    tree = ast.parse(code, CODE_NAME)
    left = dict(vars_ or {})
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id in left:
                value = left.pop(targets[0].id)
                node.value = ast.parse(repr(value), mode="eval").body
    if left:
        raise ValueError(f"no top-level variable {', '.join(sorted(left))} in the code "
                         "(vars can only change `name = value` lines at the top level)")
    return tree


def _check_vars(vars_) -> dict:
    if not isinstance(vars_, dict):
        raise ValueError("vars must be an object {name: value}")
    for k, v in vars_.items():
        if not k.isidentifier():
            raise ValueError(f"invalid variable name: {k!r}")
        if not isinstance(v, (int, float, str, bool, list, type(None))):
            raise ValueError(f"unsupported value for {k}: {v!r}")
    return vars_


def _run(code: str, vars_: dict, preamble: dict) -> dict:
    ns = {"__name__": "__model__", **preamble}
    exec(compile(_with_vars(code, _check_vars(vars_ or {})), CODE_NAME, "exec"), ns)  # noqa: S102 – that is the point
    return ns


def _named(ns: dict, convert) -> dict:
    if isinstance(ns.get("parts"), dict):
        items = ns["parts"]
    elif "result" in ns:
        items = {"model": ns["result"]}
    elif "parts" in ns:
        items = {"model": ns["parts"]}
    else:
        raise ValueError("the code must set `result` (one part) or `parts` "
                         "(a dict {'body': ..., 'lid': ...} for several parts)")
    if not items or len(items) > 20:
        raise ValueError("between 1 and 20 parts")
    out = {}
    for name, obj in items.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError(f"part names must be identifiers (letters, digits, _): {name!r}")
        out[name] = convert(name, obj)
    return out


def build123d(params: dict, workdir: Path):
    import build123d as bd
    import numpy as np

    preamble = {}
    exec("from build123d import *", preamble)  # noqa: S102
    ns = _run(params.get("code") or "", params.get("vars"), preamble)
    tol = float(params.get("tolerance", 0.02))
    ang = float(params.get("angular_tolerance", 0.1))
    if not (0.001 <= tol <= 1 and 0.01 <= ang <= 1):
        raise ValueError("tolerance must be 0.001-1 mm and angular_tolerance 0.01-1 rad")

    shapes = {}

    def convert(name, obj):
        if hasattr(obj, "part") and not isinstance(obj, bd.Shape):    # a BuildPart builder
            obj = obj.part
        if isinstance(obj, (list, tuple)):
            obj = bd.Compound(children=list(obj))
        if not isinstance(obj, bd.Shape):
            raise ValueError(f"part '{name}' is a {type(obj).__name__}, not a build123d shape "
                             "(Part, Solid, Compound or a BuildPart builder)")
        if not obj.solids():
            raise ValueError(f"part '{name}' has no solids (a 2D sketch or a line is not printable "
                             "- extrude() it)")
        shapes[name] = obj
        verts, tris = obj.tessellate(tol, ang)
        v = np.array([(p.X, p.Y, p.Z) for p in verts], dtype=np.float64).reshape(-1, 3)
        f = np.array(tris, dtype=np.int32).reshape(-1, 3)
        return v, f

    parts = _named(ns, convert)
    files = {}
    if params.get("step", True):
        labelled = []
        for name, shape in shapes.items():
            shape = shape.moved(bd.Location())       # a copy: setting the label leaves theirs alone
            shape.label = name
            labelled.append(shape)
        step = workdir / "model.step"
        bd.export_step(labelled[0] if len(labelled) == 1 else bd.Compound(children=labelled), step)
        files["model.step"] = step.read_bytes()
    return parts, files


def script(params: dict, workdir: Path):
    import numpy as np
    import trimesh

    import geometry as g
    preamble = {"np": np, "numpy": np, "trimesh": trimesh, "g": g, "json": json}
    ns = _run(params.get("code") or "", params.get("vars"), preamble)
    if isinstance(ns.get("parts"), list):
        ns["parts"] = {"model": g.union(ns["parts"])}

    def convert(name, obj):
        if not isinstance(obj, trimesh.Trimesh):
            raise ValueError(f"part '{name}' is a {type(obj).__name__}, not a trimesh.Trimesh")
        return np.asarray(obj.vertices, dtype=np.float64), np.asarray(obj.faces, dtype=np.int32)

    return _named(ns, convert), {}


def step(params: dict, workdir: Path):
    """An imported STEP file (import_file on the server), tessellated."""
    import base64

    import build123d as bd
    import numpy as np
    path = workdir / "in.step"
    path.write_bytes(base64.b64decode(params["data_base64"], validate=True))
    try:
        shape = bd.import_step(path)
    except Exception as e:  # noqa: BLE001 – OpenCascade's messages say nothing to the user
        raise ValueError(f"could not read the STEP file ({type(e).__name__})") from None
    if not shape.solids():
        raise ValueError("the STEP file has no solids")
    verts, tris = shape.tessellate(float(params.get("tolerance", 0.02)), 0.1)
    v = np.array([(p.X, p.Y, p.Z) for p in verts], dtype=np.float64).reshape(-1, 3)
    return {"model": (v, np.array(tris, dtype=np.int32).reshape(-1, 3))}, {}


KINDS = {"build123d": build123d, "script": script, "step": step}


# ---------------------------------------------------------------- the reply

def _user_trace(tb) -> str:
    """Only the frames of the user's code: the sandbox's own frames help nobody."""
    frames = [f for f in traceback.extract_tb(tb) if f.filename == CODE_NAME]
    return "".join(traceback.format_list(frames))[-2000:]


def _reply(out, header: dict, payloads: list[bytes]):
    data = json.dumps(header).encode()
    out.write(struct.pack(">I", len(data)) + data)
    for p in payloads:
        out.write(p)
    out.flush()


def main():
    _apply_limits(json.loads(os.environ["SANDBOX_LIMITS"]))
    out = os.fdopen(os.dup(1), "wb")      # the reply channel; fd 1 becomes stderr for print()
    os.dup2(2, 1)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build123d  # noqa: F401 – loaded before the job arrives (it takes seconds)
    import geometry  # noqa: F401
    work = _workdir()
    os.chdir(work)
    os.environ["HOME"] = os.environ["TMPDIR"] = str(work)
    tempfile.tempdir = str(work)
    data = sys.stdin.buffer.read()
    if not data:                 # the daemon went away (or stopped the waiting worker)
        return
    try:
        job = json.loads(data)
        if job.get("kind") not in KINDS:
            raise ValueError(f"unknown kind '{job.get('kind')}'")
        parts, files = KINDS[job["kind"]](job.get("params") or {}, work)
        if sum(len(f) for _, f in parts.values()) > MAX_TRIANGLES:
            raise ValueError(f"the model is too detailed (more than {MAX_TRIANGLES:,} triangles) "
                             "- raise the tolerance or simplify it")
        header = {"parts": [{"name": n, "nv": len(v), "nf": len(f)} for n, (v, f) in parts.items()],
                  "files": [{"name": n, "size": len(b)} for n, b in files.items()]}
        payloads = [a.tobytes() for v, f in parts.values() for a in (v, f)] + list(files.values())
    except BaseException as e:  # noqa: BLE001 – everything goes back to the server
        msg = e.args[0] if len(e.args) == 1 and isinstance(e.args[0], str) else str(e)
        if isinstance(e, MemoryError):
            msg = "the build ran out of memory - simplify the model or split it up"
        if isinstance(e, SyntaxError) and e.filename == CODE_NAME:
            msg = f"line {e.lineno}: {e.msg}"
        header, payloads = {"error": type(e).__name__, "message": msg[:2000],
                            "trace": _user_trace(e.__traceback__)}, []
    _reply(out, header, payloads)
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
