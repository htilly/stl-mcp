"""The Python sandbox (sandboxd.py + sandbox_job.py) and the build123d/script kinds that use it.

A daemon is started for the module on a temporary socket. Started as root (e.g. in a container)
it runs every job as its slot's own user, as in production; otherwise all jobs run as the test
user (no per-slot users, no RLIMIT_NPROC). The container-level isolation (no network, read-only
root, no secrets) comes from docker-compose.yml and is not tested here."""
import asyncio
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import sandbox
import server
from sandbox import SandboxError

ROOT = Path(__file__).resolve().parent.parent
pytest.importorskip("build123d")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def daemon():
    d = tempfile.mkdtemp(prefix="sbx-test-")
    os.chmod(d, 0o755)
    sock = str(Path(d) / "sandbox.sock")
    env = {**os.environ, "SANDBOX_SOCKET": sock, "SANDBOX_TIMEOUT": "15",
           "SANDBOX_SOCKET_GID": str(os.getgid()), "SANDBOX_MAX_REPLY": "20000000"}
    p = subprocess.Popen([sys.executable, str(ROOT / "sandboxd.py")], env=env, cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(300):
        if os.path.exists(sock):
            break
        time.sleep(0.1)
    else:
        p.kill()
        pytest.fail("sandboxd did not start: " + p.stdout.read().decode())
    yield sock
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


@pytest.fixture
def sbx(daemon, out_dir, monkeypatch):
    monkeypatch.setattr(sandbox, "SOCKET", daemon)
    return out_dir


BOX = """
width = 40; depth = 30
with BuildPart() as p:
    Box(width, depth, 10)
    fillet(p.edges().filter_by(Axis.Z), radius=4)
result = p
"""


def test_build123d_model_with_step(sbx):
    s = run(server.create_from_build123d("Rounded", BOX))
    part = s["parts"]["model"]
    assert s["kind"] == "build123d" and part["extents_mm"] == [40.0, 30.0, 10.0] and part["watertight"]
    assert s["step_url"].endswith(f"/files/{s['model_id']}/v1/model.step")
    step = server.store.file(s["model_id"], "v1/model.step").read_bytes()
    assert step.startswith(b"ISO-10303-21")


def test_variables_change_without_new_code(sbx):
    s = run(server.create_from_build123d("Rounded", BOX, vars={"width": 50}))
    assert s["parts"]["model"]["extents_mm"][0] == 50.0
    s = run(server.update_model(s["model_id"], {"depth": 20}))
    assert s["version"] == 2 and s["parts"]["model"]["extents_mm"] == [50.0, 20.0, 10.0]
    m = server.get_model(s["model_id"])
    assert m["params"]["vars"] == {"width": 50, "depth": 20} and "width = 40" in m["params"]["code"]
    with pytest.raises(SandboxError, match="no top-level variable height"):
        run(server.update_model(s["model_id"], {"height": 5}))


def test_several_parts(sbx):
    code = "parts = {'body': Box(10, 10, 10), 'lid': Pos(0, 0, 20) * Cylinder(5, 2)}"
    s = run(server.create_from_build123d("Two", code))
    assert set(s["parts"]) == {"body", "lid"}
    assert s["parts"]["lid"]["extents_mm"] == [10.0, 10.0, 2.0]


@pytest.mark.parametrize("code, message", [
    ("x = (", "SyntaxError: line 1"),
    ("result = Boxx(1, 1, 1)", "NameError: name 'Boxx'"),
    ("x = 1", "must set `result`"),
    ("result = Rectangle(10, 10)", "no solids"),
    ("result = 42", "not a build123d shape"),
    ("parts = {'a b': Box(1, 1, 1)}", "part names must be identifiers"),
])
def test_errors_reach_the_ai_and_save_nothing(sbx, code, message):
    with pytest.raises(SandboxError, match=message):
        run(server.create_from_build123d("Bad", code))
    assert server.list_models() == []


def test_user_traceback_only(sbx):
    with pytest.raises(SandboxError) as e:
        run(server.create_from_build123d("Bad", "a = 1\nb = a / 0\nresult = Box(1, 1, 1)"))
    msg = str(e.value)
    assert "ZeroDivisionError" in msg and 'File "<model>", line 2' in msg
    assert "sandbox_job.py" not in msg


def test_script_kind_runs_in_the_sandbox(sbx):
    s = run(server.create_from_script("Blk", "result = g.blk(10, 20, 30, 0, 0, 0)"))
    assert s["kind"] == "script" and s["parts"]["model"]["extents_mm"] == [10.0, 20.0, 30.0]
    with pytest.raises(SandboxError, match="DATABASE_URL"):    # the server's secrets are not there
        run(server.create_from_script("Env", "import os\nif 'DATABASE_URL' not in os.environ:\n"
                                             "    raise ValueError('no DATABASE_URL')"))


def test_scripts_are_for_everyone_only_with_the_sandbox(sbx, monkeypatch):
    member = SimpleNamespace(user={"role": "member"})
    server._script_allowed(member)                      # sandbox: fine
    monkeypatch.setattr(sandbox, "SOCKET", "")
    with pytest.raises(PermissionError, match="admins only"):
        server._script_allowed(member)
    server._script_allowed(SimpleNamespace(user={"role": "admin"}))


def test_limits(sbx):
    t = time.monotonic()
    with pytest.raises(SandboxError, match="longer than 15 s"):
        sandbox.build("script", {"code": "while True: pass"})
    assert time.monotonic() - t < 25
    with pytest.raises(SandboxError, match="memory"):
        sandbox.build("script", {"code": "x = bytearray(16 * 1024 ** 3)"})
    with pytest.raises(SandboxError, match="over 20 MB"):
        sandbox.build("script", {"code": "import trimesh\nresult = trimesh.creation.icosphere(8)"})
    # a process that leaves its process group does not keep the sandbox waiting: as root it is
    # killed with the rest of its slot user's processes, otherwise the build is refused
    t = time.monotonic()
    code = ("import subprocess\nsubprocess.Popen(['sleep', '60'], start_new_session=True)\n"
            "result = g.blk(1, 1, 1, 0, 0, 0)")
    if os.geteuid() == 0:
        sandbox.build("script", {"code": code})
        assert "sleep 60" not in subprocess.run(["ps", "-eo", "args"], capture_output=True,
                                                 text=True).stdout
    else:
        with pytest.raises(SandboxError, match="left processes running"):
            sandbox.build("script", {"code": code})
    assert time.monotonic() - t < 20
    # and the sandbox still works afterwards
    assert sandbox.build("build123d", {"code": "result = Box(1, 2, 3)"})["model"].extents.tolist() == [1, 2, 3]


def test_without_sandbox(out_dir, monkeypatch):
    monkeypatch.setattr(sandbox, "SOCKET", "")
    with pytest.raises(PermissionError, match="sandbox"):
        run(server.create_from_build123d("Box", "result = Box(1, 1, 1)"))


def _reply(header: dict, *payloads: bytes) -> bytes:
    import json
    h = json.dumps(header).encode()
    return struct.pack(">I", len(h)) + h + b"".join(payloads)


def test_broken_replies_are_rejected():
    v = np.zeros((3, 3)).tobytes()
    ok = {"parts": [{"name": "model", "nv": 3, "nf": 1}], "files": []}
    assert len(sandbox._parse(_reply(ok, v, np.array([[0, 1, 2]], np.int32).tobytes()))) == 1
    for reply in (_reply(ok, v, np.array([[0, 1, 3]], np.int32).tobytes()),        # index out of range
                  _reply(ok, v, np.array([[0, 1, 2]], np.int32).tobytes(), b"x"),  # trailing bytes
                  _reply(ok, v),                                                   # too short
                  _reply({"parts": [], "files": []}),                              # no parts
                  _reply({**ok, "files": [{"name": "../x.step", "size": 0}]}, v,
                         np.array([[0, 1, 2]], np.int32).tobytes()),               # unknown file
                  b"\x00\x00"):
        with pytest.raises(SandboxError, match="broken reply"):
            sandbox._parse(reply)


def test_step_import(sbx):
    import base64
    s = run(server.create_from_build123d("Rounded", BOX))
    step = server.store.file(s["model_id"], "v1/model.step").read_bytes()
    info = server.import_file("from_cad", data_base64=base64.b64encode(step).decode(), format="step")
    assert info["file"] == "from_cad.stl" and info["watertight"]
    assert info["extents_mm"] == [40.0, 30.0, 10.0]
    with pytest.raises(SandboxError, match="could not read the STEP file|no solids"):
        server.import_file("junk", data_base64=base64.b64encode(b"not a step file").decode(),
                           format="stp")
