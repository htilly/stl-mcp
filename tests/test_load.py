"""The admin Load page: CPU and memory per online user and per running build job."""
import subprocess
import sys
import threading
import time

import pytest
from starlette.testclient import TestClient

import jobs
import server


@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    return TestClient(server.app, follow_redirects=False)


def test_load_page_and_api(client):
    assert "Load" in client.get("/admin/load").text
    d = client.get("/api/admin/load").json()
    assert d["system"]["cpus"] >= 1
    assert d["server"]["pid"] > 0
    assert d["limits"]["workers"] >= 1
    assert isinstance(d["users"], list)


def test_job_counts_its_worker_process_cpu_and_memory():
    job = jobs.start({"id": "u1", "display_name": "Ann"}, "create", None)
    seen = {}

    def burn():
        p = subprocess.Popen([sys.executable, "-c", "x = bytearray(30_000_000)\nwhile True: pass"])
        jobs.add_pid(p.pid)
        try:
            time.sleep(0.4)
            jobs.snapshot()                 # first reading: the baseline
            time.sleep(0.6)
            seen.update(next(j for j in jobs.snapshot()["jobs"] if j["id"] == job.id))
        finally:
            jobs.remove_pid(p.pid)
            p.kill()
            p.wait()
    try:
        t = threading.Thread(target=jobs.run_in, args=(job, burn))
        t.start()
        t.join()
    finally:
        jobs.finish(job)
    assert seen["state"] == "running" and seen["user"] == "u1"
    assert len(seen["processes"]) == 1
    assert seen["cpu"] is not None and seen["cpu"] > 30          # the busy loop
    assert seen["mem_mb"] > 25                                     # the 30 MB bytearray
    assert all(j["id"] != job.id for j in jobs.snapshot()["jobs"])


def test_a_build_shows_up_for_its_user_while_it_runs(client, monkeypatch):
    lib = server._lib(None)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "ok"

    result = {}
    t = threading.Thread(target=lambda: result.update(r=server.anyio.run(lambda: lib.build(slow, job="test"))))
    t.start()
    try:
        assert started.wait(5)
        d = client.get("/api/admin/load").json()
        me = next(u for u in d["users"] if u["id"] == lib.user["id"])
        assert [j["label"] for j in me["jobs"]] == ["test"]
        assert me["jobs"][0]["state"] == "running"
    finally:
        release.set()
        t.join()
    assert result["r"] == "ok"
    d = client.get("/api/admin/load").json()
    assert all(not u["jobs"] for u in d["users"])


def test_load_api_is_admin_only(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    c = TestClient(server.app, follow_redirects=False)
    assert c.get("/api/admin/load").status_code == 401


def test_pulse_for_the_admin_bar(client):
    d = client.get("/api/admin/pulse").json()
    for k in ("cpu", "seconds", "cpus", "mem_total_mb", "disk_free_gb", "disk_total_gb",
              "builds_running", "builds_waiting", "online"):
        assert k in d
    assert isinstance(d["cpu"], list) and d["disk_total_gb"] >= d["disk_free_gb"] >= 0


def test_machine_cpu_is_a_percentage():
    jobs.machine_cpu()
    time.sleep(0.2)
    v = jobs.machine_cpu()
    assert v is None or 0 <= v <= 100


def test_pulse_is_admin_only(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    c = TestClient(server.app, follow_redirects=False)
    assert c.get("/api/admin/pulse").status_code == 401


def test_a_job_adds_up_its_thread_and_process_cpu(monkeypatch):
    done = []
    monkeypatch.setattr(jobs, "on_done", lambda job, cpu, wall: done.append((job.user_id, cpu, wall)))
    job = jobs.start({"id": "u1", "display_name": "Ann"}, "create", None)

    def burn():
        end = time.thread_time() + 0.3
        while time.thread_time() < end:         # the thread itself: 0.3 s
            pass
        p = jobs.Popen([sys.executable, "-c",
                        "import time\nend = time.process_time() + 0.5\n"
                        "while time.process_time() < end: pass"])
        jobs.add_pid(p.pid)
        try:
            p.wait()                             # the process: 0.5 s more (+ interpreter start)
        finally:
            jobs.remove_pid(p.pid)
    try:
        jobs.run_in(job, burn)
    finally:
        jobs.finish(job)
    [(uid, cpu, wall)] = done
    assert uid == "u1"
    assert 0.75 < cpu < 3
    assert wall >= 0.3


def test_process_cpu_before_joining_a_job_is_not_counted(monkeypatch):
    """A warm worker's imports happen before it is given a job: they are not the user's."""
    done = []
    monkeypatch.setattr(jobs, "on_done", lambda job, cpu, wall: done.append(cpu))
    p = jobs.Popen([sys.executable, "-c",
                    "import sys, time\nend = time.process_time() + 0.6\n"
                    "while time.process_time() < end: pass\nprint(flush=True)\nsys.stdin.read()"],
                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    p.stdout.readline()                          # it has burnt its 0.6 s
    job = jobs.start({"id": "u1"}, "create", None)

    def run():
        jobs.add_pid(p.pid)
        try:
            p.communicate(b"")
        finally:
            jobs.remove_pid(p.pid)
    try:
        jobs.run_in(job, run)
    finally:
        jobs.finish(job)
    assert done[0] < 0.4


def test_builds_add_up_per_user_and_day(client):
    lib = server._lib(None)
    server.anyio.run(lambda: lib.build(lambda: sum(range(200_000)), job="test"))
    server.anyio.run(lambda: lib.build(lambda: None, job="test"))
    me = next(u for u in client.get("/api/admin/users").json()["users"] if u["id"] == lib.user["id"])
    assert me["builds"] == 2 and me["builds_recent"] == 2
    assert me["cpu_seconds"] >= 0 and me["cpu_seconds_recent"] == me["cpu_seconds"]
    [day] = client.get(f"/api/admin/users/{lib.user['id']}/cpu").json()["days"]
    assert day["builds"] == 2 and len(day["day"]) == 10


def test_cpu_usage_of_unknown_users_is_skipped(db):
    server.oauth.state.record_cpu("nobody", 1.0, 1.0)
    assert server.oauth.state.cpu_stats() == {}
