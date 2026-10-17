import importlib.util
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
    monkeypatch.setenv("STL_MCP_TOKEN", "  from-env  ")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("keychain must not be used"))
    assert stl_watch.token() == "from-env"


def test_token_falls_back_to_keychain(monkeypatch):
    monkeypatch.delenv("STL_MCP_TOKEN", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="from-keychain\n", stderr=""))
    assert stl_watch.token() == "from-keychain"


@pytest.mark.parametrize("failure", ["missing", "empty"])
def test_token_missing_exits_without_printing_a_secret(monkeypatch, failure):
    monkeypatch.delenv("STL_MCP_TOKEN", raising=False)

    def fake_run(*a, **k):
        if failure == "missing":
            raise FileNotFoundError("security")
        return subprocess.CompletedProcess(a, 44, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        stl_watch.token()
    assert "STL_MCP_TOKEN" in str(e.value)


def test_base_url_defaults_and_is_overridable(monkeypatch):
    assert stl_watch.BASE.startswith("https://")
    monkeypatch.setenv("STL_MCP_URL", "http://localhost:8765/")
    spec = importlib.util.spec_from_file_location("stl_watch2", stl_watch.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.BASE == "http://localhost:8765"
