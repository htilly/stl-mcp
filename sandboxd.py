"""The sandbox for Python builds (model kinds "build123d" and "script"): a small daemon that runs
in its own container and takes jobs from the server over a Unix socket.

The container (see docker-compose.yml, service sandbox) is what keeps the code away from the
server: no network, a read-only root file system, no data volume, no secrets in its
environment, all capabilities dropped except the three this daemon needs, and memory and pid
limits. On top of that every job runs:
  - in a fresh process (sandbox_job.py, `python -I`) that serves one job and exits;
  - as the user of its slot (uid SANDBOX_UID_BASE + slot), so concurrent jobs cannot read each
    other's files or signal each other, and RLIMIT_NPROC stops fork bombs; before a slot is
    used again every process left by that uid is killed;
  - with rlimits (address space, CPU seconds, file size, open files, no core dumps) and a
    wall-clock timeout after which its whole process group is killed;
  - with a bare environment and its own private temp dir; its reply is read from a pipe and
    capped at SANDBOX_MAX_REPLY bytes.

Protocol (both directions): 4-byte big-endian length + JSON.
  request  {"kind": "build123d" | "script" | "step", "params": {...}}
  response {"ok": true, "size": n} followed by n bytes of the job's reply (sandbox_job.py)
           or {"ok": false, "error": "...", "message": "..."}

Environment:
  SANDBOX_SOCKET      socket path (default /run/sandbox/sandbox.sock)
  SANDBOX_SOCKET_GID  group that may connect (default: the group of the server's "app" user)
  SANDBOX_SLOTS       jobs at the same time (default 2); others wait up to the timeout
  SANDBOX_UID_BASE    first slot uid (default 20000); only used when started as root
  SANDBOX_TIMEOUT     wall-clock seconds per job (default 60)
  SANDBOX_MEM_MB      address-space limit per job (default 3072)
  SANDBOX_MAX_REPLY   bytes per reply (default 300 MB)
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

SOCKET = os.environ.get("SANDBOX_SOCKET", "/run/sandbox/sandbox.sock")


def _app_gid() -> int:
    try:
        import grp
        return grp.getgrnam("app").gr_gid    # the server's user in the image (see Dockerfile)
    except (ImportError, KeyError):
        return 10001


SOCKET_GID = int(os.environ.get("SANDBOX_SOCKET_GID") or _app_gid())
SLOTS = max(1, int(os.environ.get("SANDBOX_SLOTS", "2")))
UID_BASE = int(os.environ.get("SANDBOX_UID_BASE", "20000"))
TIMEOUT = float(os.environ.get("SANDBOX_TIMEOUT", "60"))
MEM_MB = int(os.environ.get("SANDBOX_MEM_MB", "3072"))
MAX_REPLY = int(os.environ.get("SANDBOX_MAX_REPLY", str(300_000_000)))
MAX_REQUEST = 150_000_000   # a STEP file to import, base64
NPROC = 64                   # processes per slot user
JOB = Path(__file__).resolve().parent / "sandbox_job.py"
AS_ROOT = os.geteuid() == 0  # else (tests, local runs) every job runs as this user

log = logging.getLogger("sandboxd")


def _limits() -> dict:
    return {"mem_mb": MEM_MB, "cpu": int(TIMEOUT * 4) + 30, "fsize": MAX_REPLY,
            "nproc": NPROC if AS_ROOT else 0}


# ---------------------------------------------------------------- slots and workers

class Slot:
    def __init__(self, i: int):
        self.i = i
        self.uid = UID_BASE + i if AS_ROOT else os.getuid()
        self.worker: subprocess.Popen | None = None


_free: list[Slot] = [Slot(i) for i in range(SLOTS)]
_free_cv = threading.Condition()


def _kill_user(uid: int):
    """Every process of a slot user, also ones that left the job's process group (setsid)."""
    if not AS_ROOT:
        return
    for _ in range(3):
        found = False
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                if d.stat().st_uid == uid:
                    found = True
                    os.kill(int(d.name), signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass
        if not found:
            return
        time.sleep(0.05)


def _spawn(slot: Slot) -> subprocess.Popen:
    """A worker that has done its imports and waits for one job on stdin."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
           "LANG": "C.UTF-8", "MALLOC_ARENA_MAX": "2", "OPENBLAS_NUM_THREADS": "1",
           "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "SANDBOX_LIMITS": json.dumps(_limits()), "SANDBOX_SLOT": str(slot.i)}
    ids = {"user": slot.uid, "group": slot.uid, "extra_groups": []} if AS_ROOT else {}
    return subprocess.Popen([sys.executable, "-I", str(JOB)], cwd="/", env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, **ids)


def _take(deadline: float) -> Slot | None:
    with _free_cv:
        while not _free:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            _free_cv.wait(left)
        return _free.pop(0)        # the one that waited longest: its worker is warm


def _give_back(slot: Slot):
    """Clears the slot and starts its next worker, so the next job does not wait for imports."""
    _kill_user(slot.uid)
    try:
        slot.worker = _spawn(slot)
    except OSError:
        log.exception("could not start a worker")
        slot.worker = None
    with _free_cv:
        _free.append(slot)
        _free_cv.notify()


def _stop(p: subprocess.Popen, uid: int | None = None):
    """Kills the job's process group and, as root, everything else its user still runs (a
    process that left the group with setsid() could otherwise keep the reply pipe open)."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if uid is not None:
        _kill_user(uid)
    p.wait()
    if p.stdin:
        p.stdin.close()      # stdout/stderr are closed by their reader threads


def _read_capped(stream, cap: int, into: list, tail: bool = False):
    """Reads a pipe to the end; keeps the first `cap` bytes (or the last, for stderr). Marks
    into[1] = True when there was more."""
    buf, over = bytearray(), False
    try:
        while chunk := stream.read1(65536):
            buf += chunk
            if len(buf) > cap:
                over = True
                buf = buf[-cap:] if tail else buf[:cap]
                if not tail:
                    break             # the reply is too big: stop reading, it gets killed
    except (OSError, ValueError):
        pass
    finally:
        into[:] = [bytes(buf), over]
        stream.close()


def _write(stream, data: bytes):
    try:
        stream.write(data)
        stream.close()
    except (BrokenPipeError, ValueError, OSError):
        pass


def run_job(request: dict) -> tuple[dict, bytes]:
    deadline = time.monotonic() + TIMEOUT
    slot = _take(deadline)
    if slot is None:
        return {"ok": False, "error": "busy",
                "message": "the sandbox is busy with other builds - try again in a minute"}, b""
    p, slot.worker = slot.worker, None
    if p is not None and p.poll() is not None:       # the waiting worker died
        _stop(p)
        for s in (p.stdout, p.stderr):
            s.close()
        p = None
    try:
        if p is None:
            p = _spawn(slot)
    except OSError as e:
        log.exception("could not start a worker")
        _give_back(slot)
        return {"ok": False, "error": "died", "message": f"the sandbox could not start a build ({e})"}, b""
    try:
        out, err = [b"", False], [b"", False]
        readers = [threading.Thread(target=_read_capped, args=(p.stdout, MAX_REPLY, out), daemon=True),
                   threading.Thread(target=_read_capped, args=(p.stderr, 4000, err, True), daemon=True)]
        # written from a thread too: a job that never reads its stdin must not block the daemon
        readers.append(threading.Thread(target=_write, args=(p.stdin, json.dumps(request).encode()),
                                        daemon=True))
        for t in readers:
            t.start()
        try:
            p.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout",
                    "message": f"the build took longer than {TIMEOUT:g} s - simplify the model "
                               "(fewer features, a coarser tolerance) or split it up"}, b""
        finally:
            _stop(p, slot.uid if AS_ROOT else None)   # whatever it left behind ends the pipes
        for t in readers:
            t.join(5)
        if any(t.is_alive() for t in readers):  # (only without per-slot users: see AS_ROOT)
            return {"ok": False, "error": "died",
                    "message": "the build left processes running - do not start background "
                               "processes"}, b""
        if out[1]:
            return {"ok": False, "error": "too_big",
                    "message": f"the build's result is over {MAX_REPLY // 1_000_000} MB - raise "
                               "the tolerance or simplify the model"}, b""
        if p.returncode != 0 or not out[0]:
            return {"ok": False, "error": "died", "message": _died(p.returncode, err[0])}, b""
        return {"ok": True, "size": len(out[0])}, out[0]
    finally:
        _give_back(slot)


def _died(rc: int, err: bytes) -> str:
    text = err.decode("utf-8", "replace")
    if rc == -signal.SIGXCPU or rc == -signal.SIGKILL and "CPU" in text:
        return "the build used too much CPU time - simplify the model or split it up"
    if "MemoryError" in text or "bad_alloc" in text or rc in (-signal.SIGSEGV, -signal.SIGBUS,
                                                              -signal.SIGABRT):
        return (f"the build crashed, most likely it needs more than {MEM_MB} MB of memory - "
                "simplify the model or split it up")
    return f"the build failed (the worker exited with {rc})\n{text[-1500:]}"


# ---------------------------------------------------------------- the socket

def _recv(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return bytes(buf)


def _send(sock: socket.socket, header: dict, body: bytes = b""):
    data = json.dumps(header).encode()
    sock.sendall(struct.pack(">I", len(data)) + data)
    if body:
        sock.sendall(body)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(30)
        try:
            (n,) = struct.unpack(">I", _recv(sock, 4))
            if n > MAX_REQUEST:
                return _send(sock, {"ok": False, "error": "bad_request", "message": "request too big"})
            request = json.loads(_recv(sock, n))
            if not isinstance(request, dict) or request.get("kind") not in ("build123d", "script", "step"):
                return _send(sock, {"ok": False, "error": "bad_request", "message": "unknown kind"})
            sock.settimeout(None)
            header, body = run_job({"kind": request["kind"], "params": request.get("params") or {}})
            sock.settimeout(60)
            _send(sock, header, body)
        except (ConnectionError, OSError, ValueError, struct.error) as e:
            log.info("dropped a connection: %s", e)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(path: str = SOCKET) -> Server:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    if AS_ROOT:
        os.setegid(SOCKET_GID)     # the socket is created in the server's group ...
    try:
        server = Server(path, Handler)
    finally:
        if AS_ROOT:
            os.setegid(0)
    os.chmod(path, 0o660)          # ... and only that group (and root) may connect
    for slot in _free:             # warm workers, one per slot
        slot.worker = _spawn(slot)
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not AS_ROOT:
        log.warning("not running as root: all jobs run as this user, without per-slot users")
    server = serve()
    log.info("SANDBOX_READY socket=%s slots=%d timeout=%gs mem=%dMB", SOCKET, SLOTS, TIMEOUT, MEM_MB)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    server.serve_forever()


if __name__ == "__main__":
    main()
