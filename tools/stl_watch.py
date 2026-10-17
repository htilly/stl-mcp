#!/usr/bin/env python3
"""Client helper for a deployed stl-mcp server: react to web UI comments as they are made.

  stl_watch.py watch                 stream /api/events; print one line per NEW open comment
  stl_watch.py comments              print every open comment (model, id, text, point, normal)
  stl_watch.py call <tool> [json]    call an MCP tool, print its text result

Run `watch` as a background monitor in a Claude Code session: each printed line wakes Claude, who
reads the comment with `comments` and answers it with update_model(..., resolves=[...]).

Configuration (nothing is ever printed):
  STL_MCP_URL    server base URL, default https://stl.tilly.se
  STL_MCP_TOKEN  the server's MCP_TOKEN. If unset, the macOS keychain item "stl-mcp-token" is used.
                 Add that once, with a hidden prompt:
                   security add-generic-password -a "$USER" -s stl-mcp-token -U -w
"""
import os
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("STL_MCP_URL", "https://stl.tilly.se").rstrip("/")


def token() -> str:
    t = os.environ.get("STL_MCP_TOKEN", "").strip()
    if t:
        return t
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", "stl-mcp-token", "-w"],
                           capture_output=True, text=True)
    except FileNotFoundError:  # not macOS
        r = None
    if r is None or r.returncode != 0 or not r.stdout.strip():
        sys.exit("no token: set STL_MCP_TOKEN or add the keychain item 'stl-mcp-token' "
                 "(see the docstring in stl_watch.py)")
    return r.stdout.strip()


def _req(path, data=None, headers=None, timeout=30):
    h = {"Authorization": f"Bearer {token()}", "User-Agent": "stl-watch/1"}
    h.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(BASE + path, body, h), timeout=timeout)


def _mcp_post(payload, sid=None):
    h = {"Accept": "application/json, text/event-stream"}
    if sid:
        h["mcp-session-id"] = sid
    with _req("/mcp", payload, h, timeout=60) as r:
        text = r.read().decode()
        new_sid = r.headers.get("mcp-session-id") or sid
    msg = None
    for line in text.splitlines():
        line = line.removeprefix("data: ").strip()
        if line.startswith("{"):
            msg = json.loads(line)
    return msg, new_sid


def call(tool, args=None):
    _, sid = _mcp_post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "stl-watch", "version": "1"}}})
    _mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    msg, _ = _mcp_post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": tool, "arguments": args or {}}}, sid)
    try:  # close the session so they do not pile up on the server
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/mcp", method="DELETE",
            headers={"Authorization": f"Bearer {token()}", "mcp-session-id": sid}), timeout=10)
    except Exception:
        pass
    res = msg["result"]
    text = "\n".join(c.get("text", "") for c in res.get("content", []))
    if res.get("isError"):
        sys.exit("tool error: " + text)
    return text


def models():
    with _req("/api/models") as r:
        return json.load(r)["models"]


def open_counts():
    return {m["id"]: m["open_comments"] for m in models()}


def cmd_comments():
    for mid, n in open_counts().items():
        if n:
            m = json.loads(call("get_model", {"model_id": mid}))
            for c in m.get("comments", []):
                print(json.dumps({"model": mid, **c}, ensure_ascii=False))


class Announcer:
    """Turns successive {model id: open comment count} snapshots into NEW_COMMENT lines.

    A line is produced when a model's count rises above what was last seen, so resolving a comment
    (count drops) and the next new comment are both handled, and edits that leave the count alone
    stay silent.
    """

    def __init__(self, base: str):
        self.base = base
        self.seen: dict[str, int] = {}

    def update(self, counts: dict[str, int]) -> list[str]:
        lines = [f"NEW_COMMENT model={mid} open={n} url={self.base}/m/{mid}"
                 for mid, n in counts.items() if n > self.seen.get(mid, 0)]
        self.seen = dict(counts)
        return lines


def cmd_watch():
    announcer = Announcer(BASE)

    def check():
        for line in announcer.update(open_counts()):
            print(line, flush=True)

    delay = 2
    while True:
        try:
            check()
            with _req("/api/events", headers={"Accept": "text/event-stream"}, timeout=90) as r:
                delay = 2
                for raw in r:  # a ping arrives every ~15 s, so 90 s of silence means dead
                    if raw.startswith(b"data:"):
                        check()
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                sys.exit(f"WATCH_STOPPED auth failed (http {e.code}) - token rotated?")
            time.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["watch"]:
        cmd_watch()
    elif a[:1] == ["comments"]:
        cmd_comments()
    elif a[:1] == ["call"] and len(a) >= 2:
        print(call(a[1], json.loads(a[2]) if len(a) > 2 else {}))
    else:
        sys.exit(__doc__)
