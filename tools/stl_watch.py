#!/usr/bin/env python3
"""Client helper for a deployed stl-mcp server: react to web UI comments as they are made.

  stl_watch.py login                 sign in once through the browser (OAuth), no token to copy
  stl_watch.py watch                 stream /api/events; print one line per NEW open comment
  stl_watch.py comments              print every open comment (model, id, text, point, normal)
  stl_watch.py call <tool> [json]    call an MCP tool, print its text result

Run `watch` as a background monitor in a Claude Code session: each printed line wakes Claude, who
reads the comment with `comments` and answers it with update_model(..., resolves=[...]).

Sign-in: run `login` once. It opens the server's normal consent page in your browser (the same
OAuth flow claude.ai uses), so the watcher acts as the account you sign in with and sees exactly
that account's models and comments. The refresh token is kept in ~/.config/stl-watch/oauth.json
(mode 0600) and renewed automatically; sign in again if it is revoked or expires (90 days idle).

Configuration (nothing is ever printed):
  STL_MCP_URL    server base URL, default https://stl.tilly.se
  STL_MCP_TOKEN  optional, overrides `login`: the server's MCP_TOKEN (admin, sees everything).
                 If unset and not signed in, the macOS keychain item "stl-mcp-token" is tried.
                 Add that once, with a hidden prompt:
                   security add-generic-password -a "$USER" -s stl-mcp-token -U -w
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

BASE = os.environ.get("STL_MCP_URL", "https://stl.tilly.se").rstrip("/")


CREDENTIALS = Path(os.environ.get("STL_WATCH_CREDENTIALS",
                                  Path.home() / ".config" / "stl-watch" / "oauth.json"))
RENEW_MARGIN = 300  # renew the access token when it has less than this many seconds left


def _load_oauth() -> dict | None:
    try:
        d = json.loads(CREDENTIALS.read_text()).get(BASE)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("refresh_token") else None


def _save_oauth(d: dict) -> None:
    try:
        all_ = json.loads(CREDENTIALS.read_text())
    except (OSError, ValueError):
        all_ = {}
    all_[BASE] = d
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREDENTIALS.with_name(CREDENTIALS.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(all_, f)
    os.replace(tmp, CREDENTIALS)


def _form(url: str, fields: dict) -> dict:
    req = urllib.request.Request(url, urllib.parse.urlencode(fields).encode(),
                                 {"User-Agent": "stl-watch/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _json(url: str, data: dict | None = None) -> dict:
    h = {"User-Agent": "stl-watch/1"}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, json.dumps(data).encode() if data is not None else None, h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _store_tokens(cred: dict, tok: dict) -> dict:
    cred = {**cred, "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token", cred["refresh_token"]),
            "expires_at": time.time() + int(tok.get("expires_in", 3600))}
    _save_oauth(cred)
    return cred


def oauth_token() -> str | None:
    """A valid OAuth access token from `login`, renewed through the refresh token; None if not signed in."""
    cred = _load_oauth()
    if not cred:
        return None
    if cred.get("access_token") and cred.get("expires_at", 0) - time.time() > RENEW_MARGIN:
        return cred["access_token"]
    try:
        tok = _form(cred["token_endpoint"], {"grant_type": "refresh_token", "client_id": cred["client_id"],
                                             "refresh_token": cred["refresh_token"]})
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):  # refresh token revoked, rotated away or expired
            sys.exit("WATCH_STOPPED sign-in expired or revoked - run `stl_watch.py login` again")
        raise  # a server hiccup: the caller retries
    return _store_tokens(cred, tok)["access_token"]


def cmd_login():
    meta = _json(BASE + "/.well-known/oauth-authorization-server")
    result: dict = {}
    state = secrets.token_urlsafe(16)

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != "/callback" or q.get("state") != [state]:
                self.send_error(404)
                return
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>autosmith watcher signed in.</h3><p>You can close this tab.</p>")

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{httpd.server_port}/callback"
    client = _json(meta["registration_endpoint"], {
        "client_name": "stl-watch", "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "stl"})
    print("Opening your browser to sign in. If it does not open, visit:\n" + url, flush=True)
    webbrowser.open(url)
    httpd.timeout = 300  # the consent request itself expires after 10 minutes
    deadline = time.time() + 300
    while not result and time.time() < deadline:
        httpd.handle_request()
    httpd.server_close()
    if "code" not in result:
        sys.exit(f"login failed: {result.get('error_description') or result.get('error') or 'timed out'}")
    tok = _form(meta["token_endpoint"], {
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect,
        "client_id": client["client_id"], "code_verifier": verifier})
    _store_tokens({"client_id": client["client_id"], "token_endpoint": meta["token_endpoint"],
                   "refresh_token": ""}, tok)
    print(f"Signed in. Credentials saved to {CREDENTIALS}")


def token() -> str:
    t = os.environ.get("STL_MCP_TOKEN", "").strip()
    if t:
        return t
    t = oauth_token()
    if t:
        return t
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", "stl-mcp-token", "-w"],
                           capture_output=True, text=True)
    except FileNotFoundError:  # not macOS
        r = None
    if r is None or r.returncode != 0 or not r.stdout.strip():
        sys.exit("not signed in: run `stl_watch.py login` (or set STL_MCP_TOKEN, "
                 "see the docstring in stl_watch.py)")
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
                sys.exit(f"WATCH_STOPPED auth failed (http {e.code}) - run `stl_watch.py login` again")
            time.sleep(delay)
            delay = min(delay * 2, 60)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["login"]:
        cmd_login()
    elif a[:1] == ["watch"]:
        cmd_watch()
    elif a[:1] == ["comments"]:
        cmd_comments()
    elif a[:1] == ["call"] and len(a) >= 2:
        print(call(a[1], json.loads(a[2]) if len(a) > 2 else {}))
    else:
        sys.exit(__doc__)
