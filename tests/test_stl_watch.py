import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "stl_watch", Path(__file__).resolve().parent.parent / "tools" / "stl_watch.py")
stl_watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stl_watch)

BASE = "https://example.test"


def test_announcer_reports_a_new_comment_once():
    a = stl_watch.Announcer(BASE)
    assert a.update({"box": 0}) == []
    assert a.update({"box": 1}) == [f"NEW_COMMENT model=box open=1 url={BASE}/m/box"]
    assert a.update({"box": 1}) == []  # unrelated change, same count: silent


def test_announcer_reports_existing_open_comments_at_startup():
    a = stl_watch.Announcer(BASE)
    assert a.update({"box": 2, "lid": 0}) == [f"NEW_COMMENT model=box open=2 url={BASE}/m/box"]


def test_announcer_handles_resolve_then_new_comment():
    a = stl_watch.Announcer(BASE)
    a.update({"box": 1})
    assert a.update({"box": 0}) == []  # resolved: silent
    assert a.update({"box": 1}) == [f"NEW_COMMENT model=box open=1 url={BASE}/m/box"]


def test_announcer_reports_second_comment_on_same_model():
    a = stl_watch.Announcer(BASE)
    a.update({"box": 1})
    assert a.update({"box": 2}) == [f"NEW_COMMENT model=box open=2 url={BASE}/m/box"]


def test_announcer_forgets_deleted_models():
    a = stl_watch.Announcer(BASE)
    a.update({"box": 1})
    a.update({})
    assert a.update({"box": 1}) == [f"NEW_COMMENT model=box open=1 url={BASE}/m/box"]


def test_token_prefers_environment(monkeypatch):
    monkeypatch.setattr(stl_watch, "URL_SET", True)
    monkeypatch.setenv("STL_MCP_TOKEN", "  from-env  ")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("keychain must not be used"))
    assert stl_watch.token() == "from-env"


def test_token_falls_back_to_keychain(monkeypatch):
    monkeypatch.setattr(stl_watch, "URL_SET", True)
    monkeypatch.setattr(stl_watch, "oauth_token", lambda: None)
    monkeypatch.delenv("STL_MCP_TOKEN", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="from-keychain\n", stderr=""))
    assert stl_watch.token() == "from-keychain"


@pytest.mark.parametrize("failure", ["missing", "empty"])
def test_token_missing_exits_without_printing_a_secret(monkeypatch, failure):
    monkeypatch.setattr(stl_watch, "URL_SET", True)
    monkeypatch.setattr(stl_watch, "oauth_token", lambda: None)
    monkeypatch.delenv("STL_MCP_TOKEN", raising=False)

    def fake_run(*a, **k):
        if failure == "missing":
            raise FileNotFoundError("security")
        return subprocess.CompletedProcess(a, 44, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        stl_watch.token()
    assert "STL_MCP_TOKEN" in str(e.value)


def test_env_and_keychain_tokens_need_an_explicit_url(monkeypatch):
    """A server's own MCP_TOKEN must never be sent to the default server by accident."""
    monkeypatch.setattr(stl_watch, "URL_SET", False)
    monkeypatch.setattr(stl_watch, "oauth_token", lambda: None)
    monkeypatch.setenv("STL_MCP_TOKEN", "from-env")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("keychain must not be used"))
    assert stl_watch.find_token() is None
    monkeypatch.setattr(stl_watch, "oauth_token", lambda: "from-login")
    assert stl_watch.find_token() == "from-login"  # the login is tied to the server it was made on


def test_base_url_defaults_and_is_overridable(monkeypatch):
    assert stl_watch.BASE.startswith("https://")
    monkeypatch.setenv("STL_MCP_URL", "http://localhost:8765/")
    spec = importlib.util.spec_from_file_location("stl_watch2", stl_watch.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.BASE == "http://localhost:8765"


# ---- OAuth login (against the real server app, HTTP routed through its test client)

import threading
import time
import urllib.parse
import urllib.request

from starlette.testclient import TestClient

import server


@pytest.fixture
def oauth_env(out_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "TOKEN", "secret")
    monkeypatch.delenv("STL_MCP_TOKEN", raising=False)
    monkeypatch.setattr(stl_watch, "CREDENTIALS", tmp_path / "cfg" / "oauth.json")
    http = TestClient(server.app, follow_redirects=False)

    def path(url):
        u = urllib.parse.urlparse(url)
        return u.path + (f"?{u.query}" if u.query else "")

    monkeypatch.setattr(stl_watch, "_json", lambda url, data=None: (
        http.post(path(url), json=data) if data is not None else http.get(path(url))).json())
    monkeypatch.setattr(stl_watch, "_form", lambda url, fields: _ok(http.post(path(url), data=fields)))
    return http, path


def _ok(r):
    if r.status_code >= 400:
        import io
        raise urllib.error.HTTPError(str(r.url), r.status_code, r.text, {}, io.BytesIO(r.content))
    return r.json()


def _approve_in_browser(http, path, monkeypatch, decision="allow"):
    """Stands in for the user: opens the authorize URL, is logged in, clicks Allow/Deny."""
    def open_(url):
        def run():
            r = http.get(path(url))
            req = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["req"][0]
            http.cookies.set(server.SESSION_COOKIE, server.oauth.state.create_session(server._admin()["id"]))
            r = http.post("/oauth/consent", data={"req": req, "decision": decision})
            urllib.request.urlopen(r.headers["location"], timeout=10).read()  # the browser follows the redirect
        threading.Thread(target=run).start()
    monkeypatch.setattr(stl_watch.webbrowser, "open", open_)


def test_login_stores_credentials_and_the_token_works(oauth_env, monkeypatch, capsys):
    http, path = oauth_env
    _approve_in_browser(http, path, monkeypatch)
    stl_watch.cmd_login()
    f = stl_watch.CREDENTIALS
    assert (f.stat().st_mode & 0o777) == 0o600
    cred = stl_watch._load_oauth()
    assert cred["client_id"] and cred["refresh_token"] and cred["access_token"]
    assert cred["refresh_token"] not in capsys.readouterr().out
    # the access token identifies the signed-in user on the real server
    r = http.get("/api/models", headers={"Authorization": f"Bearer {stl_watch.token()}"})
    assert r.status_code == 200


def test_login_denied_exits_with_the_reason(oauth_env, monkeypatch):
    http, path = oauth_env
    _approve_in_browser(http, path, monkeypatch, decision="deny")
    with pytest.raises(SystemExit) as e:
        stl_watch.cmd_login()
    assert "denied" in str(e.value)
    assert not stl_watch.CREDENTIALS.exists()


def test_token_is_renewed_before_it_expires_and_rotates_the_refresh_token(oauth_env, monkeypatch):
    http, path = oauth_env
    _approve_in_browser(http, path, monkeypatch)
    stl_watch.cmd_login()
    first = stl_watch._load_oauth()
    assert stl_watch.token() == first["access_token"]            # still fresh: no refresh
    stl_watch._save_oauth({**first, "expires_at": time.time() + 10})
    renewed = stl_watch.token()
    assert renewed != first["access_token"]
    assert stl_watch._load_oauth()["refresh_token"] != first["refresh_token"]
    assert http.get("/api/models", headers={"Authorization": f"Bearer {renewed}"}).status_code == 200


def test_revoked_sign_in_stops_the_watcher_with_a_clear_message(oauth_env, monkeypatch):
    http, path = oauth_env
    _approve_in_browser(http, path, monkeypatch)
    stl_watch.cmd_login()
    cred = stl_watch._load_oauth()
    server.oauth.state.revoke_logins(server._admin()["id"])
    stl_watch._save_oauth({**cred, "expires_at": 0})
    with pytest.raises(SystemExit) as e:
        stl_watch.token()
    assert "login" in str(e.value)


def test_environment_token_overrides_login(oauth_env, monkeypatch):
    http, path = oauth_env
    _approve_in_browser(http, path, monkeypatch)
    stl_watch.cmd_login()
    monkeypatch.setattr(stl_watch, "URL_SET", True)
    monkeypatch.setenv("STL_MCP_TOKEN", "from-env")
    assert stl_watch.token() == "from-env"


def test_if_signed_in_exits_silently_without_credentials(tmp_path):
    # no `security` on PATH, so a real keychain token on the dev machine can not leak in
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path), "STL_MCP_URL": "http://127.0.0.1:9"}
    r = subprocess.run([sys.executable, SPEC.origin, "watch", "--if-signed-in"],
                       env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_plugin_manifests_point_at_real_files():
    root = Path(__file__).resolve().parent.parent
    plugin = root / "plugins" / "stl-watch"
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    monitors = json.loads((plugin / manifest["experimental"]["monitors"]).read_text())
    assert all(m["name"] and m["command"] and m["description"] for m in monitors)
    assert "scripts/stl_watch.py" in monitors[0]["command"] and (plugin / "scripts" / "stl_watch.py").exists()
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    assert (root / market["plugins"][0]["source"] / ".claude-plugin" / "plugin.json").exists()
