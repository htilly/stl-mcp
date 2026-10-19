"""Sandboxed OpenSCAD builds (model kind "openscad").

OpenSCAD cannot run programs or write files on its own; what it can do is *read* files, through
import(), surface(), dxf_dim()/dxf_cross() and include/use. So the code is checked before it runs:
those functions are rejected and include/use may only name files in the bundled libraries
(<BOSL2/...>, <NopSCADlib/...>, ...: every directory in OPENSCAD_LIBS is one library). The checker tokenizes the code the way OpenSCAD's lexer does, so strings and
comments cannot hide anything and nothing hidden in them is rejected by mistake.

On top of that each build runs:
  - in a fresh, empty temp dir as cwd (and HOME), with OPENSCADPATH pointing only at the bundled
    read-only libraries dir, so a relative include can only find those;
  - as its own process group with a timeout (the whole group is killed when it runs out);
  - with rlimits: address space, CPU seconds, file size (caps the STL and the logs), open files
    (on Linux all are enforced; other systems apply the ones they support, macOS ignores the
    address-space limit);
  - at most OPENSCAD_CONCURRENCY builds at a time across the server, so parallel requests cannot
    multiply the memory limit;
  - with stdout/stderr going to files whose tail is read back, so a chatty echo() loop cannot
    fill the server's memory.

Multiple parts: list part names in params["parts"]; the code is built once per part with
PART set to that name (`if (PART == "lid") ...`).

Environment:
  OPENSCAD_BIN      the binary (default: `openscad` on PATH)
  OPENSCAD_LIBS     the libraries dir (default /opt/openscad-libs, else ./openscad-libs)
  OPENSCAD_TIMEOUT  seconds per build, all parts together (default 60)
  OPENSCAD_MEM_MB   address-space limit per process (default 2048)
  OPENSCAD_BACKEND  geometry engine, passed as --backend when the binary has that option
                    (default manifold: much faster than CGAL; empty = the binary's default)
  OPENSCAD_CONCURRENCY  builds running at the same time, server-wide (default 2); a build that
                    cannot start within OPENSCAD_TIMEOUT fails with "busy"
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import trimesh

import jobs

BIN = os.environ.get("OPENSCAD_BIN") or shutil.which("openscad") or ""
_default_libs = Path("/opt/openscad-libs")
LIBS = Path(os.environ.get("OPENSCAD_LIBS") or
            (_default_libs if _default_libs.is_dir() else Path(__file__).parent / "openscad-libs"))
TIMEOUT = float(os.environ.get("OPENSCAD_TIMEOUT", "60"))
MEM_MB = int(os.environ.get("OPENSCAD_MEM_MB", "2048"))
BACKEND = os.environ.get("OPENSCAD_BACKEND", "manifold")
CONCURRENCY = max(1, int(os.environ.get("OPENSCAD_CONCURRENCY", "2")))
MAX_OUTPUT = 50_000_000    # bytes, per written file (STL or log); ~1M triangles of binary STL
MAX_TOTAL_STL = 150_000_000  # bytes of STL over all parts, loaded into the server's memory
MAX_CODE = 200_000         # characters of source
LOG_TAIL = 2000            # characters of OpenSCAD's messages returned on errors

# functions/modules that read files from disk
FORBIDDEN = {"import", "surface", "dxf_dim", "dxf_cross"}
# plus every library module/function that (directly or through others) calls one of them, e.g.
# BOSL2's builtins.scad `_surface(file)` – found by scanning each library the code includes (and
# the libraries that one includes), see _forbidden() and _library_forbidden(). Per library, so a
# name one library wraps around import() does not block the same name in another.
# include <...> / use <...>: only files inside a bundled library, <library dir>/<file>.scad
LIBRARY_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
LIBRARY_PATH = re.compile(rf"({LIBRARY_NAME.pattern})/[A-Za-z0-9_][A-Za-z0-9_./-]*\.scad")
_INCLUDE = re.compile(r"\b(?:include|use)[ \t\r\n>]*<([^\t\r\n>]*)>")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def available() -> bool:
    return bool(BIN) and Path(BIN).exists()


@functools.lru_cache(maxsize=4)
def _has_backend_option(binary: str) -> bool:
    """2021.01 has no --backend (only CGAL); the nightlies list it in --help."""
    try:
        r = subprocess.run([binary, "--help"], capture_output=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "QT_QPA_PLATFORM": "offscreen"})
    except (OSError, subprocess.TimeoutExpired):
        return False
    return b"--backend" in r.stdout + r.stderr


def _backend_args() -> list[str]:
    return [f"--backend={BACKEND}"] if BACKEND and _has_backend_option(BIN) else []


class OpenSCADError(ValueError):
    pass


# ---------------------------------------------------------------- source check

_SKIP = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', re.S)   # comments and strings
_TOKEN = re.compile(r"[A-Za-z0-9_$]+|==|!=|<=|>=|[=(){}\[\];]")
_OPEN, _CLOSE = "([{", ")]}"


def _definitions(src: str):
    """(name, names it calls) for each module, function and function literal (`f = function`)
    in OpenSCAD source. Over-approximates on purpose (a nested definition's calls count for the
    outer one too): a false positive only blocks a name, a miss would let a file read through."""
    t = _TOKEN.findall(_SKIP.sub(" ", src))
    for i, w in enumerate(t):
        if w in ("module", "function") and i + 2 < len(t) and NAME.fullmatch(t[i + 1]) and t[i + 2] == "(":
            name, j = t[i + 1], i + 2
        elif w == "=" and i > 0 and NAME.fullmatch(t[i - 1]) and t[i + 1:i + 2] == ["function"]:
            name, j = t[i - 1], i + 1
        else:
            continue
        # the body runs to the ';' (or the closing brace of a { } body) at its own nesting level,
        # continuing through `else`; it also ends where an enclosing bracket closes
        depth, body = 0, []
        while j < len(t):
            tok = t[j]
            body.append(tok)
            if tok in _OPEN:
                depth += 1
            elif tok in _CLOSE:
                depth -= 1
                if depth < 0:
                    break
            if depth == 0 and (tok == ";" or (tok == "}" and w == "module")) \
                    and not (j + 1 < len(t) and t[j + 1] == "else"):
                break
            j += 1
        yield name, {a for a, b in zip(body, body[1:]) if b == "("}


@functools.lru_cache(maxsize=4)
def _forbidden(libs: Path) -> frozenset:
    """FORBIDDEN plus every name the bundled libraries define on top of them, transitively."""
    defs: dict[str, set] = {}
    if libs.is_dir():
        files = (Path(d) / f for d, _, fs in os.walk(libs, followlinks=True) for f in fs)
        for f in sorted(f for f in files if f.suffix == ".scad"):
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for name, words in _definitions(src):
                defs.setdefault(name, set()).update(words)
    bad = set(FORBIDDEN)
    while True:
        more = {name for name, words in defs.items() if name not in bad and words & bad}
        if not more:
            return frozenset(bad)
        bad |= more


def libraries() -> list[str]:
    """The bundled libraries: the directories in LIBS."""
    try:
        return sorted(d.name for d in LIBS.iterdir() if d.is_dir() and LIBRARY_NAME.fullmatch(d.name))
    except OSError:
        return []


@functools.lru_cache(maxsize=4)
def _dependencies(libs: Path) -> dict:
    """{library: libraries its files include}, resolving each include/use the way OpenSCAD does:
    next to the including file first, then on OPENSCADPATH (= libs)."""
    deps: dict[str, set] = {}
    root = os.path.abspath(libs)
    for lib in (d for d in sorted(libs.iterdir()) if d.is_dir()) if libs.is_dir() else ():
        found = deps.setdefault(lib.name, set())
        for d, _, fs in os.walk(lib, followlinks=True):
            for f in (Path(d) / f for f in fs if f.endswith(".scad")):
                try:
                    src = _SKIP.sub(" ", f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                for path in _INCLUDE.findall(src):
                    for target in (f.parent / path, libs / path):
                        if target.is_file():
                            rel = Path(os.path.relpath(os.path.normpath(os.path.abspath(target)), root))
                            if rel.parts and rel.parts[0] not in (lib.name, ".."):
                                found.add(rel.parts[0])
                            break
    return deps


def _library_forbidden(libs: Path, used) -> frozenset:
    """FORBIDDEN plus the file-reader wrappers of the used libraries and everything they include."""
    deps, todo, seen = _dependencies(libs), list(used), set()
    while todo:
        lib = todo.pop()
        if lib not in seen:
            seen.add(lib)
            todo += deps.get(lib, ())
    bad = set(FORBIDDEN)
    for lib in seen:
        bad |= _forbidden(libs / lib)
    return frozenset(bad)


def check(code: str) -> list[str]:
    """Rejects code that could read files. Returns the include/use paths it found."""
    if len(code) > MAX_CODE:
        raise OpenSCADError(f"the code is too long (max {MAX_CODE} characters)")
    if "\0" in code:
        raise OpenSCADError("the code contains a NUL character")
    paths, words, i, n = [], [], 0, len(code)
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
            words.append(word)
            if word in ("include", "use"):
                # OpenSCAD's lexer: include[ \t\r\n>]*<path>
                m = re.compile(r"[ \t\r\n>]*<([^\t\r\n>]*)>").match(code, i)
                if not m:
                    raise OpenSCADError(f"'{word}' must be followed by <library/file.scad>")
                path = m.group(1)
                if not LIBRARY_PATH.fullmatch(path) or ".." in path.split("/") \
                        or path.split("/")[0] not in libraries():
                    raise OpenSCADError(f"{word} <{path}> is not allowed: only the bundled "
                                        "libraries can be used, e.g. include <BOSL2/std.scad> "
                                        f"(bundled: {', '.join(libraries()) or 'none'})")
                if not (LIBS / path).is_file():
                    raise OpenSCADError(f"{word} <{path}>: no such library file")
                paths.append(path)
                i = m.end()
        else:
            i += 1
    forbidden = _library_forbidden(LIBS, {p.split("/")[0] for p in paths})
    for word in words:
        if word in forbidden:
            raise OpenSCADError(f"{word}() is not allowed (it reads files from the server)")
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
            try:
                resource.setrlimit(lim, (val, val))
            except (ValueError, OSError):
                if sys.platform == "linux":   # Linux supports them all: a failure is a real error
                    raise
    return apply


def _run(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "OPENSCADPATH": str(LIBS),
           "LANG": "C.UTF-8", "QT_QPA_PLATFORM": "offscreen", "TMPDIR": str(cwd)}
    log = cwd / "log.txt"
    with open(log, "wb") as out:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True,
                             preexec_fn=_limits(int(timeout) + 5) if os.name == "posix" else None)
        jobs.add_pid(p.pid)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with _quiet():
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            raise OpenSCADError(f"OpenSCAD took longer than {timeout:g} s – simplify the model "
                                "(lower $fn, fewer parts) or split it up") from None
        finally:
            jobs.remove_pid(p.pid)
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
    if not _slots.acquire(timeout=TIMEOUT):
        raise OpenSCADError("the server is busy with other OpenSCAD builds – try again in a minute")
    try:
        return _build(code, overrides, parts, time.monotonic() + TIMEOUT)
    finally:
        _slots.release()


_slots = threading.BoundedSemaphore(CONCURRENCY)


def _build(code: str, overrides: list[str], parts: list[str], deadline: float) -> dict:
    out, total = {}, 0
    with tempfile.TemporaryDirectory(prefix="scad-") as td:
        tdir = Path(td)
        src = tdir / "model.scad"
        src.write_text(code, encoding="utf-8")
        for part in parts or ["model"]:
            left = deadline - time.monotonic()
            if left <= 0:
                raise OpenSCADError(f"OpenSCAD took longer than {TIMEOUT:g} s")
            stl = tdir / f"{part}.stl"
            argv = [BIN, *_backend_args(), "-o", str(stl), *overrides,
                    *(["-D", f"PART={_literal(part)}"] if parts else []), str(src)]
            rc, log = _run(argv, tdir, left)
            if rc != 0 or not stl.is_file() or stl.stat().st_size == 0:
                msg = log.strip() or f"openscad exited with {rc}"
                if "Current top level object is empty" in log or "not 3D" in log:
                    msg += "\n(the code produced no 3D object" + (f" for PART == \"{part}\"" if parts else "") + ")"
                raise OpenSCADError(msg[-LOG_TAIL:])
            size = stl.stat().st_size
            total += size
            if size > MAX_OUTPUT or total > MAX_TOTAL_STL:
                raise OpenSCADError(f"the model is too detailed (STL over {MAX_OUTPUT // 1_000_000} MB "
                                    f"per part or {MAX_TOTAL_STL // 1_000_000} MB in all) – lower $fn")
            mesh = trimesh.load(stl, force="mesh")
            if mesh.is_empty:
                raise OpenSCADError(f"part '{part}' is empty")
            out[part] = mesh
    return out
