"""Sandboxed OpenSCAD builds (model kind "openscad").

OpenSCAD cannot run programs or write files on its own; what it can do is *read* files, through
import(), surface(), dxf_dim()/dxf_cross() and include/use. So the code is checked before it runs:
those functions are rejected and include/use may only name files in the bundled libraries
(<BOSL2/...>). The checker tokenizes the code the way OpenSCAD's lexer does, so strings and
comments cannot hide anything and nothing hidden in them is rejected by mistake.

On top of that each build runs:
  - in a fresh, empty temp dir as cwd (and HOME), with OPENSCADPATH pointing only at the bundled
    read-only libraries dir, so a relative include can only find those;
  - as its own process group with a timeout (the whole group is killed when it runs out);
  - with rlimits on Linux: address space, CPU seconds, file size (caps the STL and the logs),
    open files;
  - with stdout/stderr going to files whose tail is read back, so a chatty echo() loop cannot
    fill the server's memory.

Multiple parts: list part names in params["parts"]; the code is built once per part with
PART set to that name (`if (PART == "lid") ...`).

Environment:
  OPENSCAD_BIN      the binary (default: `openscad` on PATH)
  OPENSCAD_LIBS     the libraries dir (default /opt/openscad-libs, else ./openscad-libs)
  OPENSCAD_TIMEOUT  seconds per build, all parts together (default 60)
  OPENSCAD_MEM_MB   address-space limit per process on Linux (default 2048)
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import trimesh

BIN = os.environ.get("OPENSCAD_BIN") or shutil.which("openscad") or ""
_default_libs = Path("/opt/openscad-libs")
LIBS = Path(os.environ.get("OPENSCAD_LIBS") or
            (_default_libs if _default_libs.is_dir() else Path(__file__).parent / "openscad-libs"))
TIMEOUT = float(os.environ.get("OPENSCAD_TIMEOUT", "60"))
MEM_MB = int(os.environ.get("OPENSCAD_MEM_MB", "2048"))
MAX_OUTPUT = 200_000_000   # bytes, per written file (STL or log)
MAX_CODE = 200_000         # characters of source
LOG_TAIL = 2000            # characters of OpenSCAD's messages returned on errors

# functions/modules that read files from disk
FORBIDDEN = {"import", "surface", "dxf_dim", "dxf_cross"}
# include <...> / use <...>: only files inside a bundled library
LIBRARY_PATH = re.compile(r"(BOSL2)/[A-Za-z0-9_][A-Za-z0-9_./-]*\.scad")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def available() -> bool:
    return bool(BIN) and Path(BIN).exists()


class OpenSCADError(ValueError):
    pass


# ---------------------------------------------------------------- source check

def check(code: str) -> list[str]:
    """Rejects code that could read files. Returns the include/use paths it found."""
    if len(code) > MAX_CODE:
        raise OpenSCADError(f"the code is too long (max {MAX_CODE} characters)")
    if "\0" in code:
        raise OpenSCADError("the code contains a NUL character")
    paths, i, n = [], 0, len(code)
    while i < n:
        c = code[i]
        if code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j + 1
        elif code.startswith("/*", i):
            j = code.find("*/", i + 2)
            if j < 0:
                raise OpenSCADError("unterminated /* comment")
            i = j + 2
        elif c == '"':
            i += 1
            while i < n and code[i] != '"':
                i += 2 if code[i] == "\\" else 1
            if i >= n:
                raise OpenSCADError("unterminated string")
            i += 1
        elif c.isdigit() or (c == "." and i + 1 < n and code[i + 1].isdigit()):
            m = re.compile(r"[0-9.]*([eE][+-]?[0-9]+)?").match(code, i)
            i = max(m.end(), i + 1)
        elif c.isalpha() or c in "_$":
            m = re.compile(r"[A-Za-z0-9_$]+").match(code, i)
            word, i = m.group(), m.end()
            if word in FORBIDDEN:
                raise OpenSCADError(f"{word}() is not allowed (it reads files from the server)")
            if word in ("include", "use"):
                # OpenSCAD's lexer: include[ \t\r\n>]*<path>
                m = re.compile(r"[ \t\r\n>]*<([^\t\r\n>]*)>").match(code, i)
                if not m:
                    raise OpenSCADError(f"'{word}' must be followed by <BOSL2/...>")
                path = m.group(1)
                if not LIBRARY_PATH.fullmatch(path) or ".." in path.split("/"):
                    raise OpenSCADError(f"{word} <{path}> is not allowed: only the bundled "
                                        "libraries can be used, e.g. include <BOSL2/std.scad>")
                if not (LIBS / path).is_file():
                    raise OpenSCADError(f"{word} <{path}>: no such library file")
                paths.append(path)
                i = m.end()
        else:
            i += 1
    return paths


def _literal(v) -> str:
    """A JSON value as an OpenSCAD literal (for -D overrides)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            raise OpenSCADError("variable values must be finite numbers")
        return repr(v)
    if isinstance(v, str):
        if any(ord(ch) < 32 for ch in v):
            raise OpenSCADError("variable strings may not contain control characters")
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ",".join(_literal(x) for x in v) + "]"
    if v is None:
        return "undef"
    raise OpenSCADError(f"unsupported variable value: {v!r}")


def _overrides(variables: dict) -> list[str]:
    args = []
    for k, v in (variables or {}).items():
        if not NAME.fullmatch(k) or k == "PART":
            raise OpenSCADError(f"invalid variable name: {k!r}")
        args += ["-D", f"{k}={_literal(v)}"]
    return args


# ---------------------------------------------------------------- running

def _limits(cpu: int):
    def apply():
        import resource
        mem = MEM_MB * 1024 * 1024
        for lim, val in ((resource.RLIMIT_AS, mem), (resource.RLIMIT_CPU, cpu),
                         (resource.RLIMIT_FSIZE, MAX_OUTPUT), (resource.RLIMIT_NOFILE, 256),
                         (resource.RLIMIT_CORE, 0)):
            resource.setrlimit(lim, (val, val))
    return apply


def _run(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "OPENSCADPATH": str(LIBS),
           "LANG": "C.UTF-8", "QT_QPA_PLATFORM": "offscreen", "TMPDIR": str(cwd)}
    log = cwd / "log.txt"
    with open(log, "wb") as out:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True,
                             preexec_fn=_limits(int(timeout) + 5) if sys.platform == "linux" else None)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with _quiet():
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            raise OpenSCADError(f"OpenSCAD took longer than {timeout:g} s – simplify the model "
                                "(lower $fn, fewer parts) or split it up") from None
        finally:
            with _quiet():                    # nothing it started may outlive it
                os.killpg(p.pid, signal.SIGKILL)
    with open(log, "rb") as f:
        f.seek(max(0, log.stat().st_size - LOG_TAIL))
        return rc, f.read().decode("utf-8", "replace")


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return exc[0] is not None and issubclass(exc[0], (ProcessLookupError, PermissionError))


def build(params: dict) -> dict:
    """params: {"code": str, "vars": {name: value}, "parts": [name, ...]} -> {part: Trimesh}."""
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise OpenSCADError("params.code must be OpenSCAD source")
    check(code)
    if not available():
        raise PermissionError("OpenSCAD is not installed on this server")
    overrides = _overrides(params.get("vars") or {})
    parts = params.get("parts") or []
    if not isinstance(parts, list) or not all(isinstance(p, str) and NAME.fullmatch(p) for p in parts):
        raise OpenSCADError("parts must be a list of names (letters, digits, _)")
    if len(set(parts)) != len(parts) or len(parts) > 20:
        raise OpenSCADError("parts must be unique (max 20)")
    deadline = time.monotonic() + TIMEOUT
    out = {}
    with tempfile.TemporaryDirectory(prefix="scad-") as td:
        tdir = Path(td)
        src = tdir / "model.scad"
        src.write_text(code, encoding="utf-8")
        for part in parts or ["model"]:
            left = deadline - time.monotonic()
            if left <= 0:
                raise OpenSCADError(f"OpenSCAD took longer than {TIMEOUT:g} s")
            stl = tdir / f"{part}.stl"
            argv = [BIN, "-o", str(stl), *overrides,
                    *(["-D", f"PART={_literal(part)}"] if parts else []), str(src)]
            rc, log = _run(argv, tdir, left)
            if rc != 0 or not stl.is_file() or stl.stat().st_size == 0:
                msg = log.strip() or f"openscad exited with {rc}"
                if "Current top level object is empty" in log or "not 3D" in log:
                    msg += "\n(the code produced no 3D object" + (f" for PART == \"{part}\"" if parts else "") + ")"
                raise OpenSCADError(msg[-LOG_TAIL:])
            mesh = trimesh.load(stl, force="mesh")
            if mesh.is_empty:
                raise OpenSCADError(f"part '{part}' is empty")
            out[part] = mesh
    return out
