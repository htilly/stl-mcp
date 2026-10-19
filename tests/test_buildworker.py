"""Builds run in a sandboxed worker process: same meshes as an in-process build, the same errors,
and a build that is too slow or too big is stopped without hurting the server."""
import io
import os
import signal
import sys
import time

import pytest

import buildworker
import csg
import geometry as g

linux = pytest.mark.skipif(sys.platform != "linux", reason="rlimits are only applied on Linux")

SPEC = {"vars": {"w": 40, "wall": 2}, "parts": {
    "body": {"op": "difference", "children": [
        {"shape": "box", "size": ["=w", "=w", 20], "radius": 3},
        {"shape": "cylinder", "d": "=w-2*wall", "h": 30, "translate": ["=w/2", "=w/2", "=wall"]}]},
    "lid": {"shape": "box", "size": ["=w", "=w", 2], "translate": [50, 0, 0]}}}
# many seconds of work on every core, in little memory
SLOW = {"root": {"op": "union", "children": [{"shape": "sphere", "d": 3, "segments": 48,
                                              "array": {"count": [12, 12, 12], "spacing": [2, 2, 2]}}]}}
# gigabytes of memory within a fraction of a second
BIG = {"root": {"op": "minkowski", "children": [
    {"shape": "sphere", "d": 10, "segments": 400}, {"shape": "sphere", "d": 10, "segments": 400}]}}


def stl(mesh) -> bytes:
    buf = io.BytesIO()
    mesh.export(buf, file_type="stl")
    return buf.getvalue()


@pytest.fixture
def fresh(monkeypatch):
    """No waiting worker (so changed limits apply to the next one); afterwards none is left."""
    buildworker.shutdown()
    monkeypatch.setattr(buildworker, "WARM", 0)
    spawned = []
    real = buildworker._spawn

    def spawn():
        p = real()
        spawned.append(p)
        return p
    monkeypatch.setattr(buildworker, "_spawn", spawn)
    yield spawned
    buildworker.shutdown()


def gone(p) -> bool:
    try:
        os.killpg(p.pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_csg_builds_are_identical_to_in_process_builds():
    got, want = buildworker.build("csg", SPEC), csg.build(SPEC)
    assert list(got) == list(want) == ["body", "lid"]
    for name in want:
        assert stl(got[name]) == stl(want[name])
        assert (got[name].faces == want[name].faces).all()


def test_enclosure_and_primitive_builds_are_identical():
    params = {"inner_x": 50, "inner_y": 30, "inner_z": 20, "wall": 2,
              "cutouts": [{"wall": "front", "pos": 10, "width": 8, "height": 5}]}
    got = buildworker.build("enclosure", params)
    body, lid = g.enclosure(50, 30, 20, wall=2, floor=2.4, lid_t=2.4, screws=True,
                            cutouts=params["cutouts"])
    assert stl(got["body"]) == stl(body) and stl(got["lid"]) == stl(lid)
    prim = g.primitive("cylinder", diameter=10, height=5)
    prim.apply_translation([0, 0, -prim.bounds[0][2]])
    got = buildworker.build("primitive", {"shape": "cylinder", "diameter": 10, "height": 5})
    assert stl(got["model"]) == stl(prim)


def test_mesh_node_matches_and_empty_is_none():
    node = {"shape": "box", "size": [1, 2, 3], "translate": [5, 0, 0]}
    want = csg.to_trimesh(csg.build_node(node, {}))
    assert stl(buildworker.mesh_node(node, {})) == stl(want)
    empty = {"op": "intersection", "children": [
        {"shape": "box", "size": [1, 1, 1]}, {"shape": "box", "size": [1, 1, 1], "translate": [5, 0, 0]}]}
    assert buildworker.mesh_node(empty, {}) is None


def test_errors_come_back_with_their_type_and_message():
    with pytest.raises(ValueError, match="unknown shape 'nope'"):
        buildworker.build("csg", {"root": {"shape": "nope"}})
    with pytest.raises(ValueError, match="unknown variable 'b'"):
        buildworker.build("csg", {"vars": {"a": "=b"}, "root": {"shape": "box", "size": [1, 1, 1]}})
    with pytest.raises(KeyError) as e:
        buildworker.build("enclosure", {})
    assert e.value.args == ("inner_x",)
    with pytest.raises(ValueError, match="unknown kind"):
        buildworker.build("script", {"code": "1"})


def test_unexpected_errors_are_not_shown_as_user_messages():
    with pytest.raises(RuntimeError, match="AttributeError"):   # an internal error, logged
        buildworker.build("primitive", {"shape": 5})


def test_a_slow_build_is_killed_at_the_timeout(fresh):
    with pytest.raises(buildworker.BuildError, match="took longer than 1 s"):
        buildworker.build("csg", SLOW, timeout=1)
    assert fresh and all(gone(p) for p in fresh)


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_the_worker_has_no_secrets_in_its_environment(fresh, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    p = buildworker._take()
    try:
        # Popen returns once exec has begun; the kernel fills in environ a moment later
        deadline = time.monotonic() + 5
        while not (environ := open(f"/proc/{p.pid}/environ", "rb").read()) \
                and p.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert b"DATABASE_URL" not in environ and b"BUILD_LIMITS" in environ
    finally:
        buildworker._cleanup(p)


def test_a_dead_worker_is_reported_as_a_build_error(fresh, monkeypatch):
    real = buildworker._job_in

    def kill_first(p, job, timeout):
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        return real(p, job, timeout)
    monkeypatch.setattr(buildworker, "_job_in", kill_first)
    with pytest.raises(buildworker.BuildError, match="the build failed"):
        buildworker.build("csg", SPEC)


def test_died_explains_cpu_and_memory():
    assert "CPU time" in str(buildworker._died(-signal.SIGXCPU, ""))
    assert "memory" in str(buildworker._died(1, "Traceback ...\nMemoryError\n"))
    assert "memory" in str(buildworker._died(-signal.SIGABRT, "std::bad_alloc"))
    assert "crashed" in str(buildworker._died(-signal.SIGSEGV, ""))
    assert "exited with 3" in str(buildworker._died(3, ""))


@linux
def test_cpu_limit_stops_a_build(fresh, monkeypatch):
    monkeypatch.setattr(buildworker, "CPU_SECONDS", 1)
    with pytest.raises(buildworker.BuildError, match="CPU time"):
        buildworker.build("csg", SLOW, timeout=60)


@linux
def test_memory_limit_stops_a_build(fresh, monkeypatch):
    monkeypatch.setattr(buildworker, "MEM_MB", 400)
    assert buildworker.build("csg", SPEC)    # an ordinary model fits easily
    with pytest.raises(buildworker.BuildError, match="memory"):
        buildworker.build("csg", BIG, timeout=60)
    assert buildworker.build("csg", SPEC)    # the next build gets a fresh worker and works


def test_waiting_workers_are_reused_and_replaced(monkeypatch):
    buildworker.shutdown()
    monkeypatch.setattr(buildworker, "WARM", 1)
    buildworker.build("csg", SPEC)              # starts one to keep waiting
    waiting = list(buildworker._warm)
    assert len(waiting) == 1 and waiting[0].poll() is None
    buildworker.build("csg", SPEC)              # uses it (one job per process) and starts another
    assert waiting[0].poll() is not None
    assert len(buildworker._warm) == 1 and buildworker._warm[0] is not waiting[0]
    buildworker.shutdown()
    assert not buildworker._warm and gone(waiting[0])
