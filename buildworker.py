"""Sandboxed builds of the recipe kinds csg, enclosure and primitive (and the editor's node meshes).

A recipe is data, not code, but it can still ask for a lot of work: deep booleans, big arrays,
minkowski sums. csg.py caps expressions, numbers and triangle counts; this module makes sure that
whatever slips past those caps costs one throwaway process and not the server. Each build runs in
a fresh worker process (one job per process, so nothing carries over between users):
  - started with `python -I` and a bare environment (no DATABASE_URL, tokens or SSO secrets),
    an empty temp dir as cwd, HOME and TMPDIR;
  - as its own process group with a wall-clock timeout (the whole group is killed when it runs
    out, and always after the job, so nothing it started can outlive it);
  - with rlimits on Linux, set by the worker on itself before it reads the job: address space,
    CPU seconds, file size, open files, no core dumps;
  - handing the meshes back as raw vertex/face arrays (.npy), so the server gets exactly the
    mesh the builder made.

One worker is kept started and waiting (imports done), so a build does not wait for Python
and manifold3d to load.

Environment:
  BUILD_TIMEOUT      wall-clock seconds per build (default 60)
  BUILD_CPU_SECONDS  CPU seconds per build on Linux, all threads together (default 4 x timeout;
                     manifold3d uses every core, so a big union burns several CPU seconds a second)
  BUILD_MEM_MB       address-space limit per build on Linux (default 3072)
  BUILD_WARM         workers kept started and waiting (default 1, 0 = start one per build)
  BUILD_WORKERS      builds running at the same time, server-wide (default 4; the rest wait)
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
import trimesh

TIMEOUT = float(os.environ.get("BUILD_TIMEOUT", "60"))
CPU_SECONDS = int(os.environ.get("BUILD_CPU_SECONDS", "0")) or int(4 * TIMEOUT)
MEM_MB = int(os.environ.get("BUILD_MEM_MB", "3072"))
WARM = int(os.environ.get("BUILD_WARM", "1"))
WORKERS = int(os.environ.get("BUILD_WORKERS", "4"))
MAX_OUTPUT = 1_000_000_000  # bytes per written file; 5M triangles are ~180 MB
MAX_REPLY = 1_000_000       # bytes of the worker's JSON reply
KINDS = ("csg", "enclosure", "primitive", "relief")

# Exceptions a build raises with messages meant for the user; they are raised again here with
# the same type and message. Anything else becomes a RuntimeError (an "internal error").
_ERRORS = {e.__name__: e for e in (ValueError, KeyError, TypeError, IndexError, ZeroDivisionError,
                                   OverflowError, RecursionError, PermissionError)}


class BuildError(ValueError):
    """The build was stopped: too slow, too much memory, or the worker died."""


# ---------------------------------------------------------------- server side

_lock = threading.Lock()
_warm: list[subprocess.Popen] = []
_running = threading.BoundedSemaphore(WORKERS)


def _limits() -> dict:
    return {"cpu": CPU_SECONDS, "mem_mb": MEM_MB, "fsize": MAX_OUTPUT}


def _spawn() -> subprocess.Popen:
    """A worker that has done its imports and waits for one job on stdin."""
    home = tempfile.mkdtemp(prefix="build-")
    env = {"PATH": "/usr/bin:/bin", "HOME": home, "TMPDIR": home, "LANG": "C.UTF-8",
           # glibc reserves 64 MB of address space per thread arena, which would eat the
           # RLIMIT_AS budget on machines with many cores
           "MALLOC_ARENA_MAX": "2",
           # numpy's BLAS reserves big buffers per thread and is not what builds need
           "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
           "BUILD_LIMITS": json.dumps(_limits())}
    import jobs  # here, not at the top: the worker (python -I) cannot import it before its path is set
    # jobs.Popen: reaped with wait4, so the job it runs is charged its CPU time
    p = jobs.Popen([sys.executable, "-I", str(Path(__file__).resolve())], cwd=home, env=env,
                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   start_new_session=True)
    p.home = home
    return p


def _take() -> subprocess.Popen:
    with _lock:
        while _warm:
            p = _warm.pop()
            if p.poll() is None:
                break
            _cleanup(p)
        else:
            p = None
        while len(_warm) < WARM:
            _warm.append(_spawn())
    return p or _spawn()


def _kill(p: subprocess.Popen):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _cleanup(p: subprocess.Popen):
    _kill(p)
    p.wait()
    for s in (p.stdin, p.stdout, p.stderr):
        if s:
            s.close()
    shutil.rmtree(p.home, ignore_errors=True)


def _died(rc: int, err: str) -> BuildError:
    if rc == -signal.SIGXCPU:
        return BuildError(f"the build used more than {CPU_SECONDS} s of CPU time – simplify the "
                          "model (lower segments, fewer copies, fewer booleans) or split it up")
    if "MemoryError" in err or "bad_alloc" in err:
        return BuildError(f"the build needs more than {MEM_MB} MB of memory – simplify the model "
                          "(lower segments, fewer copies) or split it up")
    if rc in (-signal.SIGSEGV, -signal.SIGBUS, -signal.SIGABRT):
        # what manifold3d does when an allocation fails under RLIMIT_AS
        return BuildError(f"the build crashed, most likely it needs more than {MEM_MB} MB of "
                          "memory – simplify the model (lower segments, fewer copies) or split it up")
    return BuildError(f"the build failed (the worker exited with {rc})")


def _job(job: dict, timeout: float | None = None) -> list[tuple[str, trimesh.Trimesh]]:
    timeout = TIMEOUT if timeout is None else timeout
    with _running:
        return _job_in(_take(), job, timeout)


def _job_in(p: subprocess.Popen, job: dict, timeout: float) -> list[tuple[str, trimesh.Trimesh]]:
    import jobs  # here, not at the top: the worker (python -I) cannot import it before its path is set
    jobs.add_pid(p.pid)
    try:
        try:
            out, err = p.communicate(json.dumps(job).encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BuildError(f"the build took longer than {timeout:g} s – simplify the model "
                             "(lower segments, fewer copies, fewer booleans) or split it up") from None
        except BrokenPipeError:      # it died before reading the job
            out, err = b"", b""
            p.wait()
        err = err[-4000:].decode("utf-8", "replace")
        try:
            reply = json.loads(out[:MAX_REPLY]) if p.returncode == 0 else None
        except ValueError:
            reply = None
        if reply is None:
            raise _died(p.returncode, err)
        if "result" in reply and "error" not in reply:      # a job that returns data, not meshes
            return reply["result"]
        if "error" in reply:
            if reply["error"] == "MemoryError":
                raise _died(0, "MemoryError")
            cls = _ERRORS.get(reply["error"])
            if cls is None:
                raise RuntimeError(f"build failed: {reply['error']}: {reply['message']}\n"
                                   f"{reply.get('trace', '')}")
            raise cls(reply["message"])
        home = Path(p.home)
        parts = []
        for i, name in enumerate(reply["parts"]):
            v = np.load(home / f"{i}.v.npy", allow_pickle=False)
            f = np.load(home / f"{i}.f.npy", allow_pickle=False)
            parts.append((name, trimesh.Trimesh(vertices=v, faces=f, process=False)))
        return parts
    finally:
        jobs.remove_pid(p.pid)
        _cleanup(p)


def build(kind: str, params: dict, timeout: float | None = None) -> dict[str, trimesh.Trimesh]:
    """Builds a csg / enclosure / primitive recipe in a worker -> {part name: mesh}."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind '{kind}'")
    return dict(_job({"op": "build", "kind": kind, "params": params}, timeout))


def import_outline(data: bytes, fmt: str, timeout: float | None = None) -> dict:
    """An SVG/DXF file as an outline path (outline.import_file), parsed in a worker: a crafted
    file costs a throwaway process, not the server."""
    import base64
    return _job({"op": "outline", "data": base64.b64encode(data).decode(), "format": fmt}, timeout)


def mesh_node(node, vars_: dict, timeout: float | None = None) -> trimesh.Trimesh | None:
    """One CSG node in its own frame (the editor's preview), or None when it is empty."""
    parts = _job({"op": "node", "node": node, "vars": vars_}, timeout)
    return parts[0][1] if parts else None


def shutdown():
    """Stops the waiting workers (tests, server exit)."""
    with _lock:
        while _warm:
            _cleanup(_warm.pop())


# ---------------------------------------------------------------- worker side

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


def _run_job(job: dict) -> list:
    import csg
    import geometry as g
    if job["op"] == "outline":
        import base64

        import outline
        return outline.import_file(base64.b64decode(job["data"]), job["format"])
    if job["op"] == "node":
        m = csg.build_node(job["node"], csg.resolve_vars(job.get("vars") or {}))
        return [] if m.is_empty() else [("node", csg.to_trimesh(m))]
    kind, p = job["kind"], dict(job["params"])
    if kind == "csg":
        return list(csg.build(p).items())
    if kind == "enclosure":
        body, lid = g.enclosure(
            p.pop("inner_x"), p.pop("inner_y"), p.pop("inner_z"),
            wall=p.pop("wall", 2.4), floor=p.pop("floor", 2.4), lid_t=p.pop("lid_thickness", 2.4),
            screws=p.pop("corner_screws", True), **p)
        return [("body", body), ("lid", lid)]
    if kind == "relief":
        import relief
        return [(k, csg.to_trimesh(m)) for k, m in relief.build(p).items()]
    if kind == "primitive":
        mesh = g.primitive(p.pop("shape"), **p)
        mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
        return [("model", mesh)]
    raise ValueError(f"unknown kind '{kind}'")


def _worker():
    _apply_limits(json.loads(os.environ["BUILD_LIMITS"]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import csg  # noqa: F401 – loaded before the job arrives
    import geometry  # noqa: F401
    data = sys.stdin.buffer.read()
    if not data:                 # the server went away (or shut down the waiting worker)
        return
    try:
        parts = _run_job(json.loads(data))
        if isinstance(parts, dict):             # op "outline": plain data
            sys.stdout.write(json.dumps({"result": parts}))
            return
        for i, (_, mesh) in enumerate(parts):
            np.save(f"{i}.v.npy", np.asarray(mesh.vertices))
            np.save(f"{i}.f.npy", np.asarray(mesh.faces))
        reply = {"parts": [name for name, _ in parts]}
    except BaseException as e:  # noqa: BLE001 – everything goes back to the server
        import traceback
        msg = e.args[0] if len(e.args) == 1 and isinstance(e.args[0], str) else str(e)
        reply = {"error": type(e).__name__, "message": msg,
                 "trace": traceback.format_exc()[-2000:]}
    sys.stdout.write(json.dumps(reply))


if __name__ == "__main__":
    _worker()
