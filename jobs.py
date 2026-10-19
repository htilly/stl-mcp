"""Running builds and what they cost right now: CPU and memory per job and per user (admin UI).

A job is one Lib.build call: it waits for a slot, then runs in a server thread, which may start
sandboxed worker processes (buildworker, OpenSCAD, scripts). The thread and the processes register
here, and a snapshot reads their CPU time and memory from /proc (Linux) or `ps` (elsewhere).
CPU % is measured between two snapshots (the admin page polls), so 100 % is one core busy and a
multi-threaded build can show more. A thread has no memory of its own: only the processes count.
"""
from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
import time
from collections import deque

_lock = threading.Lock()
_jobs: dict[int, "Job"] = {}
_ids = itertools.count(1)
_local = threading.local()
# ("p", pid) / ("t", tid) -> (cpu seconds, when, last rate)
_prev: dict[tuple, tuple[float, float, float | None]] = {}
_TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
LINUX = sys.platform == "linux" and os.path.isdir("/proc/self/task")


class Job:
    def __init__(self, user: dict, label: str, model: str | None):
        self.id = next(_ids)
        self.user_id, self.user_name = user["id"], user.get("display_name") or ""
        self.label, self.model = label, model
        self.created = time.time()
        self.started: float | None = None
        self.tid: int | None = None
        self.pids: set[int] = set()


def start(user: dict, label: str, model: str | None = None) -> Job:
    """A job waiting for a build slot."""
    job = Job(user, label, model)
    with _lock:
        _jobs[job.id] = job
    return job


def finish(job: Job):
    with _lock:
        _jobs.pop(job.id, None)
        _prev.pop(("t", job.tid), None)
        for pid in job.pids:
            _prev.pop(("p", pid), None)


def run_in(job: Job, fn, *a, **kw):
    """Runs fn in the calling (worker) thread as this job: processes it starts are counted."""
    job.tid, job.started = threading.get_native_id(), time.time()
    _baseline(("t", job.tid))
    _local.job = job
    try:
        return fn(*a, **kw)
    finally:
        _local.job = None


def add_pid(pid: int):
    """A worker process started for the job running in this thread (if any)."""
    job = getattr(_local, "job", None)
    if job is not None:
        with _lock:
            job.pids.add(pid)
        _baseline(("p", pid))


def remove_pid(pid: int):
    job = getattr(_local, "job", None)
    if job is not None:
        with _lock:
            job.pids.discard(pid)
            _prev.pop(("p", pid), None)


# ---------------------------------------------------------------- measuring

def _proc_stat(path: str) -> float | None:
    """utime + stime in seconds from a /proc .../stat file."""
    try:
        with open(path) as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return (int(rest[11]) + int(rest[12])) / _TICK
    except (OSError, IndexError, ValueError):
        return None


def _ps(pids) -> dict[int, tuple[float, int]]:
    """{pid: (cpu seconds, rss bytes)} via ps (macOS and other systems without /proc)."""
    pids = [str(p) for p in pids]
    if not pids:
        return {}
    try:
        out = subprocess.run(["ps", "-o", "pid=,time=,rss=", "-p", ",".join(pids)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    res = {}
    for line in out.splitlines():
        try:
            pid, t, rss = line.split()
            days, _, t = t.rpartition("-")
            secs = sum(float(x) * 60 ** i for i, x in enumerate(reversed(t.split(":"))))
            res[int(pid)] = (secs + 86400 * int(days or 0), int(rss) * 1024)
        except ValueError:
            continue
    return res


def _cpu(key: tuple) -> float | None:
    kind, n = key
    if LINUX:
        return _proc_stat(f"/proc/self/task/{n}/stat" if kind == "t" else f"/proc/{n}/stat")
    if kind == "p":
        return _ps([n]).get(n, (None, 0))[0]
    return None  # a single thread's CPU time is only readable from /proc


def _rss(pid: int) -> int | None:
    if LINUX:
        try:
            with open(f"/proc/{pid}/statm") as f:
                return int(f.read().split()[1]) * _PAGE
        except (OSError, IndexError, ValueError):
            return None
    return _ps([pid]).get(pid, (0, None))[1]


def _baseline(key: tuple):
    cpu = _cpu(key)
    if cpu is not None:
        with _lock:
            _prev[key] = (cpu, time.monotonic(), None)


def _rate(key: tuple, cpu: float | None, now: float) -> float | None:
    """CPU % since the previous reading of this thread/process (100 = one core). Readings closer
    than 0.5 s (two admins polling) get the last rate, so each measures a useful interval."""
    if cpu is None:
        return None
    last = _prev.get(key)
    if last is not None and now - last[1] < 0.5:
        return last[2]
    rate = None if last is None else max(0.0, (cpu - last[0]) / (now - last[1]) * 100)
    _prev[key] = (cpu, now, rate)
    return rate


def system() -> dict:
    out = {"cpus": os.cpu_count(), "load": None, "mem_total_mb": None, "mem_available_mb": None}
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            info = {line.split(":")[0]: int(line.split()[1]) for line in f if ":" in line}
        out["mem_total_mb"] = round(info["MemTotal"] / 1024)
        out["mem_available_mb"] = round(info.get("MemAvailable", info.get("MemFree", 0)) / 1024)
    except (OSError, KeyError, ValueError, IndexError):
        try:  # no /proc (macOS): the total at least
            out["mem_total_mb"] = round(os.sysconf("SC_PHYS_PAGES") * _PAGE / 2 ** 20)
        except (ValueError, OSError, AttributeError):
            pass
    return out


def snapshot() -> dict:
    """{system, server, jobs}: the machine, the server process and every running/waiting job."""
    now, wall = time.monotonic(), time.time()
    with _lock:
        jobs = [(j, sorted(j.pids)) for j in _jobs.values()]
    me = os.getpid()
    ps = {} if LINUX else _ps([me, *(p for _, pids in jobs for p in pids)])

    def proc(pid):
        if LINUX:
            return _cpu(("p", pid)), _rss(pid)
        return ps.get(pid, (None, None))

    rows = []
    with _lock:
        cpu, rss = proc(me)
        server = {"pid": me, "cpu": _rate(("p", me), cpu, now),
                  "mem_mb": round(rss / 1e6, 1) if rss else None}
        for j, pids in jobs:
            row = {"id": j.id, "user": j.user_id, "user_name": j.user_name, "label": j.label,
                   "model": j.model, "state": "running" if j.started else "waiting",
                   "seconds": round(wall - (j.started or j.created), 1),
                   "thread_cpu": None, "processes": [], "cpu": None, "mem_mb": 0.0}
            if j.tid is not None and j.started:
                row["thread_cpu"] = _rate(("t", j.tid), _cpu(("t", j.tid)), now)
            for pid in pids:
                c, r = proc(pid)
                row["processes"].append({"pid": pid, "cpu": _rate(("p", pid), c, now),
                                         "mem_mb": round(r / 1e6, 1) if r else None})
            parts = [row["thread_cpu"], *(p["cpu"] for p in row["processes"])]
            if any(x is not None for x in parts):
                row["cpu"] = round(sum(x or 0 for x in parts), 1)
            row["mem_mb"] = round(sum(p["mem_mb"] or 0 for p in row["processes"]), 1)
            if row["thread_cpu"] is not None:
                row["thread_cpu"] = round(row["thread_cpu"], 1)
            for p in row["processes"]:
                if p["cpu"] is not None:
                    p["cpu"] = round(p["cpu"], 1)
            rows.append(row)
        if server["cpu"] is not None:
            server["cpu"] = round(server["cpu"], 1)
    return {"system": system(), "server": server, "jobs": rows}


# ---------------------------------------------------------------- the machine's CPU over time

HISTORY = 60          # samples kept: 5 minutes at PULSE_SECONDS
PULSE_SECONDS = 5
_history: deque = deque(maxlen=HISTORY)   # (unix time, machine CPU % 0-100)
_sampler: threading.Thread | None = None
_last_stat: tuple[int, int] | None = None  # (busy, total) jiffies from /proc/stat


def machine_cpu() -> float | None:
    """The whole machine's CPU use in % (0-100, all cores) since the previous call: /proc/stat on
    Linux; elsewhere the 1-minute load average per core, capped at 100."""
    global _last_stat
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
        total = sum(vals[:8])
        prev, _last_stat = _last_stat, (total - idle, total)
        if prev is None or total <= prev[1]:
            return None
        return round(100 * (total - idle - prev[0]) / (total - prev[1]), 1)
    except (OSError, ValueError, IndexError):
        try:
            return round(min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100), 1)
        except OSError:
            return None


def _sample_forever():
    while True:
        cpu = machine_cpu()
        if cpu is not None:
            _history.append((round(time.time()), cpu))
        time.sleep(PULSE_SECONDS)


def cpu_history() -> list[tuple[int, float]]:
    """The machine's CPU % over the last few minutes; starts the sampler on first use."""
    global _sampler
    with _lock:
        if _sampler is None:
            machine_cpu()                                    # the baseline for the first delta
            _sampler = threading.Thread(target=_sample_forever, name="cpu-pulse", daemon=True)
            _sampler.start()
    return list(_history)


def counts() -> dict:
    """Running and waiting jobs, without measuring anything."""
    with _lock:
        running = sum(1 for j in _jobs.values() if j.started)
        return {"running": running, "waiting": len(_jobs) - running,
                "users": sorted({j.user_id for j in _jobs.values()})}
