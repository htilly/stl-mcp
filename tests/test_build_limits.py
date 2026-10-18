"""Per-user limits on builds: how many per minute (MCP and web), how many at once, and that a
slow build does not hold up other models' changes."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import server

BOX = {"vars": {"w": 20, "h": 10}, "root": {"shape": "box", "size": ["=w", 20, "=h"]}}


def ctx(user):
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(user=user))))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    st = server.oauth.state
    ann, bob = st.create_user("Ann", "ann@example.com"), st.create_user("Bob", "bob@example.com")

    def client_for(user):
        c = TestClient(server.app, follow_redirects=False)
        c.cookies.set(server.SESSION_COOKIE, st.create_session(user["id"]))
        return c
    return SimpleNamespace(ann=ann, bob=bob, client_for=client_for)


def test_mcp_builds_are_rate_limited_per_user(world, monkeypatch):
    monkeypatch.setattr(server, "build_hits", server.RateLimiter(3, 60))
    m = run(server.create_model("a", BOX, ctx=ctx(world.ann)))
    run(server.update_model(m["model_id"], {"w": 30}, ctx=ctx(world.ann)))
    run(server.update_model(m["model_id"], name="renamed", ctx=ctx(world.ann)))  # no build: free
    run(server.create_enclosure("e", 20, 20, 10, ctx=ctx(world.ann)))
    with pytest.raises(server.RateLimitError, match="too many builds"):
        run(server.update_model(m["model_id"], {"w": 40}, ctx=ctx(world.ann)))
    with pytest.raises(server.RateLimitError):
        run(server.create_model("b", BOX, ctx=ctx(world.ann)))
    run(server.create_model("bobs", BOX, ctx=ctx(world.bob)))     # someone else is not affected


def test_web_builds_and_previews_get_429(world, monkeypatch):
    monkeypatch.setattr(server, "build_hits", server.RateLimiter(2, 60))
    monkeypatch.setattr(server, "mesh_hits", server.RateLimiter(2, 60))
    mid = run(server.create_model("a", BOX, ctx=ctx(world.ann)))["model_id"]   # 1 of 2
    c = world.client_for(world.ann)
    up = {"action": "update", "params": BOX}
    assert c.post(f"/api/models/{mid}", json=up).status_code == 200            # 2 of 2
    r = c.post(f"/api/models/{mid}", json=up)
    assert r.status_code == 429 and "too many builds" in r.json()["error"]
    assert c.post(f"/api/models/{mid}", json={"action": "revert", "version": 1}).status_code == 429
    # renaming does not build
    assert c.post(f"/api/models/{mid}", json={"action": "rename", "name": "x"}).status_code == 200
    node = {"node": {"shape": "box", "size": [1, 2, 3]}, "vars": {}}
    for _ in range(2):
        r = c.post(f"/api/models/{mid}/mesh", json=node)
        assert r.status_code == 200 and r.content
    r = c.post(f"/api/models/{mid}/mesh", json=node)
    assert r.status_code == 429 and "too many previews" in r.json()["error"]


def test_a_user_runs_at_most_builds_at_once_and_the_rest_wait(world, monkeypatch):
    monkeypatch.setattr(server, "BUILDS_AT_ONCE", 1)
    running, peak = {}, {}
    lock = threading.Lock()

    def work(uid):
        with lock:
            running[uid] = running.get(uid, 0) + 1
            peak[uid] = max(peak.get(uid, 0), running[uid])
        time.sleep(0.2)
        with lock:
            running[uid] -= 1
        return uid

    async def main():
        ann, bob = server.Lib(world.ann), server.Lib(world.bob)
        t0 = time.monotonic()
        got = await asyncio.gather(*(ann.build(work, "ann") for _ in range(3)), bob.build(work, "bob"))
        return got, time.monotonic() - t0
    got, took = run(main())
    assert got == ["ann", "ann", "ann", "bob"]
    assert peak == {"ann": 1, "bob": 1}
    assert took >= 0.55                       # Ann's three ran one after the other
    assert not server._build_slots            # nothing is left behind


def test_too_many_waiting_builds_are_refused(world, monkeypatch):
    monkeypatch.setattr(server, "BUILDS_AT_ONCE", 1)
    monkeypatch.setattr(server, "BUILDS_WAITING", 2)

    async def main():
        lib = server.Lib(world.ann)
        return await asyncio.gather(*(lib.build(time.sleep, 0.1) for _ in range(3)),
                                    return_exceptions=True)
    got = run(main())
    assert sum(isinstance(r, server.RateLimitError) for r in got) == 1
    assert not server._build_slots


def test_a_slow_build_does_not_block_other_models(out_dir):
    store = server.store
    store.create("fast", "csg", BOX)
    started, release = threading.Event(), threading.Event()
    real = store.build

    def build(kind, params):
        if params.get("vars", {}).get("w") == 99:
            started.set()
            release.wait(10)
        return real(kind, params)
    store.build = build
    t = threading.Thread(target=store.create, args=("slow", "csg", {**BOX, "vars": {"w": 99, "h": 1}}))
    t.start()
    try:
        assert started.wait(10)
        t0 = time.monotonic()
        store.update("fast", changes={"w": 25})      # does not wait for the slow build
        assert time.monotonic() - t0 < 5
        assert store.current(store.get("fast"))["params"]["vars"]["w"] == 25
    finally:
        release.set()
        t.join()
    assert store.get("slow")["current"] == 1


def test_a_change_that_lands_during_a_build_is_not_lost(out_dir):
    store = server.store
    store.create("box", "csg", BOX)
    real, calls = store._built, []

    def built(kind, params):
        calls.append(params["vars"])
        if len(calls) == 1:                          # someone else saves meanwhile
            store.update("box", changes={"h": 7})
        return real(kind, params)
    store._built = built
    store.update("box", changes={"w": 30})
    cur = store.current(store.get("box"))
    assert cur["version"] == 3
    assert cur["params"]["vars"] == {"w": 30, "h": 7}    # built again on top of the other change
