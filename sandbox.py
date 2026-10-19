"""Client for the Python sandbox (sandboxd.py): builds of the model kinds "build123d" and "script".

The server never runs that code itself; it sends it over SANDBOX_SOCKET to the sandbox
container and reads back plain arrays (vertices, faces) and files (the STEP export).

Environment:
  SANDBOX_SOCKET  the daemon's socket (unset = the sandbox kinds are not available)
"""
from __future__ import annotations

import json
import os
import socket
import struct

import numpy as np
import trimesh

SOCKET = os.environ.get("SANDBOX_SOCKET", "")
TIMEOUT = float(os.environ.get("SANDBOX_TIMEOUT", "60"))
KINDS = ("build123d", "script")
MAX_FILE = 100_000_000


class SandboxError(ValueError):
    """The code failed, or the sandbox stopped it (too slow, too much memory, too big)."""


class Built(dict):
    """{part name: mesh}, plus the files the build made next to the meshes ({"model.step": bytes})."""

    def __init__(self, parts, files=None):
        super().__init__(parts)
        self.files = files or {}


def available() -> bool:
    return bool(SOCKET) and os.path.exists(SOCKET)


def _recv(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise SandboxError("the sandbox closed the connection")
        buf += chunk
    return bytes(buf)


def _call(kind: str, params: dict) -> bytes:
    if not available():
        raise PermissionError(f"{kind} models need the sandbox, which is not set up on this "
                              "server (SANDBOX_SOCKET)")
    data = json.dumps({"kind": kind, "params": params}).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(TIMEOUT * 2 + 30)     # it may first wait for a free slot
        try:
            s.connect(SOCKET)
            s.sendall(struct.pack(">I", len(data)) + data)
            (n,) = struct.unpack(">I", _recv(s, 4))
            header = json.loads(_recv(s, n))
            if not header.get("ok"):
                raise SandboxError(header.get("message") or header.get("error") or "the build failed")
            return _recv(s, int(header["size"]))
        except (socket.timeout, ConnectionError, FileNotFoundError) as e:
            raise SandboxError(f"the sandbox did not answer ({e}) - try again in a minute") from None


def _parse(reply: bytes) -> Built:
    """The job's reply (see sandbox_job.py), checked against its own sizes."""
    try:
        (n,) = struct.unpack_from(">I", reply)
        header = json.loads(reply[4:4 + n])
    except (struct.error, ValueError):
        raise SandboxError("the sandbox sent a broken reply") from None
    if "error" in header:
        msg = f"{header['error']}: {header.get('message', '')}"
        if header.get("trace"):
            msg += f"\n{header['trace']}"
        raise SandboxError(msg)
    pos, parts, files = 4 + n, {}, {}
    try:
        for p in header["parts"]:
            nv, nf = int(p["nv"]), int(p["nf"])
            v = np.frombuffer(reply, np.float64, nv * 3, pos).reshape(nv, 3)
            pos += nv * 24
            f = np.frombuffer(reply, np.int32, nf * 3, pos).reshape(nf, 3)
            pos += nf * 12
            if nf == 0 or f.min() < 0 or f.max() >= nv or not np.isfinite(v).all():
                raise ValueError
            mesh = trimesh.Trimesh(vertices=v.copy(), faces=f.astype(np.int64), process=False)
            mesh.merge_vertices()       # OpenCascade tessellates face by face
            parts[str(p["name"])] = mesh
        for fl in header.get("files", []):
            size = int(fl["size"])
            if fl["name"] not in ("model.step",) or size > MAX_FILE or pos + size > len(reply):
                raise ValueError
            files[fl["name"]] = reply[pos:pos + size]
            pos += size
    except (ValueError, KeyError, TypeError):
        raise SandboxError("the sandbox sent a broken reply") from None
    if pos != len(reply) or not parts:
        raise SandboxError("the sandbox sent a broken reply")
    return Built(parts, files)


def build(kind: str, params: dict) -> Built:
    if kind not in KINDS:
        raise ValueError(f"unknown kind '{kind}'")
    return _parse(_call(kind, params))


def import_step(data: bytes) -> trimesh.Trimesh:
    """A STEP file as a mesh. OpenCascade reads it in the sandbox: a crafted file then crashes
    or hangs a throwaway process there, not the server."""
    import base64
    return _parse(_call("step", {"data_base64": base64.b64encode(data).decode()}))["model"]
