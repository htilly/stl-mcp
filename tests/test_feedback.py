"""The feedback button: admin settings (encrypted keys), Claude's draft, the GitHub issue."""
import json

import httpx
import pytest
from starlette.testclient import TestClient

import feedback
import server

ADMIN = {"Authorization": "Bearer secret"}
SETUP = {"enabled": True, "anthropic_key": "sk-ant-secret-1234", "github_token": "ghp_secret_5678"}


class Fake:
    """Stands in for api.anthropic.com and api.github.com."""

    def __init__(self):
        self.requests = []
        self.model_text = json.dumps({"title": "Add a ruler", "body": "## What\nA ruler.", "label": "enhancement"})
        self.anthropic = 200
        self.github = 201

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == feedback.ANTHROPIC_URL:
            if self.anthropic != 200:
                return httpx.Response(self.anthropic, json={"error": "x"})
            return httpx.Response(200, json={"content": [{"type": "text", "text": self.model_text}]})
        if request.method == "GET":
            return httpx.Response(200 if self.github == 201 else self.github, json={})
        if self.github != 201:
            return httpx.Response(self.github, json={"message": "no"})
        return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/htilly/stl-mcp/issues/7"})

    def sent(self, host):
        return [r for r in self.requests if r.url.host == host]


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(feedback, "http", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    return f


@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    monkeypatch.setattr(server, "SETTINGS_KEY", "test-settings-key")
    return TestClient(server.app, follow_redirects=False)


def login(name="Joe"):
    """Request headers of a logged-in user (new session each call, user created once)."""
    st, email = server.oauth.state, f"{name.lower()}@example.com"
    user = st.user_by_email(email)
    user = user or st.create_user(name, email)
    return {"Cookie": f"{server.SESSION_COOKIE}={st.create_session(user['id'])}"}


def setup(client, **extra):
    r = client.post("/api/admin/feedback", json={**SETUP, **extra}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["feedback"]


# ---- admin settings

def test_admin_only(client):
    assert client.get("/api/admin/feedback").status_code == 401
    assert client.get("/api/admin/feedback", headers=login()).status_code == 403
    assert client.post("/api/admin/feedback", json=SETUP, headers=login()).status_code == 403


def test_secrets_are_stored_encrypted_and_never_returned(client):
    v = setup(client)
    assert v["anthropic_key"] == {"set": True, "tail": "1234"}
    assert v["github_token"] == {"set": True, "tail": "5678"}
    again = client.get("/api/admin/feedback", headers=ADMIN)
    assert "sk-ant-secret" not in again.text and "ghp_secret" not in again.text
    stored = server.db.one("SELECT value FROM server_settings WHERE key = 'feedback'")["value"]
    assert "secret" not in json.dumps(stored)
    assert feedback.decrypt("test-settings-key", stored["anthropic_key"]) == "sk-ant-secret-1234"


def test_empty_key_keeps_the_saved_one_and_clear_removes_it(client):
    setup(client)
    v = client.post("/api/admin/feedback", json={"anthropic_key": "", "model": "m"}, headers=ADMIN).json()["feedback"]
    assert v["anthropic_key"]["set"] and v["model"] == "m"
    v = client.post("/api/admin/feedback", json={"clear": ["github_token"]}, headers=ADMIN).json()["feedback"]
    assert not v["github_token"]["set"] and v["anthropic_key"]["set"]


def test_keys_need_settings_key(client, monkeypatch):
    monkeypatch.setattr(server, "SETTINGS_KEY", "")
    r = client.post("/api/admin/feedback", json=SETUP, headers=ADMIN)
    assert r.status_code == 400 and "SETTINGS_KEY" in r.json()["error"]
    assert client.get("/api/admin/feedback", headers=ADMIN).json()["feedback"]["can_store_secrets"] is False


def test_wrong_settings_key_cannot_read_saved_keys(client, monkeypatch):
    setup(client)
    monkeypatch.setattr(server, "SETTINGS_KEY", "another-key")
    v = client.get("/api/admin/feedback", headers=ADMIN).json()["feedback"]
    assert v["anthropic_key"] == {"set": True, "tail": ""}


@pytest.mark.parametrize("body", [{"repo": "not a repo"}, {"per_hour": 0}, {"per_hour": "x"},
                                  {"labels": 5}, {"clear": ["enabled"]}, {"nope": 1},
                                  {"anthropic_key": "has space"}])
def test_bad_settings_are_refused(client, body):
    assert client.post("/api/admin/feedback", json=body, headers=ADMIN).status_code == 400


def test_connection_test(client, fake):
    setup(client)
    assert client.post("/api/admin/feedback/test", headers=ADMIN).json() == {"anthropic": "ok", "github": "ok"}
    fake.anthropic, fake.github = 401, 404
    out = client.post("/api/admin/feedback/test", headers=ADMIN).json()
    assert out["anthropic"] == "the key was refused" and "htilly/stl-mcp" in out["github"]


# ---- the button's two steps

def test_button_hidden_until_set_up(client, fake):
    assert client.get("/api/auth", headers=ADMIN).json()["feedback"] is False
    setup(client, enabled=False)
    assert client.get("/api/auth", headers=ADMIN).json()["feedback"] is False
    setup(client, enabled=True)
    assert client.get("/api/auth", headers=ADMIN).json()["feedback"] is True


def test_refuses_when_not_set_up(client, fake):
    r = client.post("/api/feedback/refine", json={"text": "hello there"}, headers=login())
    assert r.status_code == 404
    assert not fake.requests


def test_login_required(client, fake):
    setup(client)
    assert client.post("/api/feedback/refine", json={"text": "hello there"}).status_code == 401
    assert client.post("/api/feedback", json={"title": "x"}).status_code == 401


def test_cross_origin_refused(client, fake):
    setup(client)
    r = client.post("/api/feedback/refine", json={"text": "hello there"},
                    headers={**login(), "Origin": "https://evil.example"})
    assert r.status_code == 403 and not fake.requests


def test_refine_returns_a_draft_and_creates_nothing(client, fake):
    setup(client)
    r = client.post("/api/feedback/refine", json={"text": "please add a ruler to the viewer", "page": "/m/abc"},
                    headers=login())
    assert r.status_code == 200
    assert r.json() == {"title": "Add a ruler", "body": "## What\nA ruler.", "label": "enhancement",
                        "labels": ["bug", "enhancement", "question"], "refined": True}
    [call] = fake.sent("api.anthropic.com")
    assert call.headers["x-api-key"] == "sk-ant-secret-1234"
    sent = json.loads(call.content)
    assert "please add a ruler" in sent["messages"][0]["content"] and "enhancement" in sent["system"]
    assert not fake.sent("api.github.com")


def test_label_must_be_on_the_admins_list(client, fake):
    setup(client)
    fake.model_text = 'Sure! {"title": "T", "body": "B", "label": "wontfix; delete everything"}'
    r = client.post("/api/feedback/refine", json={"text": "something is off"}, headers=login())
    assert r.json()["label"] == "" and r.json()["title"] == "T"


@pytest.mark.parametrize("answer", ["not json at all", "[]", '{"title": "", "body": ""}'])
def test_bad_model_answer_falls_back_to_the_users_words(client, fake, answer):
    setup(client)
    fake.model_text = answer
    r = client.post("/api/feedback/refine", json={"text": "The lid does not fit.\nIt is 1 mm off."},
                    headers=login()).json()
    assert r["refined"] is False and r["title"] == "The lid does not fit"
    assert r["body"].endswith("It is 1 mm off.") and r["label"] == "bug"


def test_model_down_falls_back(client, fake):
    setup(client)
    fake.anthropic = 529
    r = client.post("/api/feedback/refine", json={"text": "The lid does not fit"}, headers=login())
    assert r.status_code == 200 and r.json()["refined"] is False


@pytest.mark.parametrize("text", ["", "ab", "x" * (feedback.MAX_TEXT + 1)])
def test_text_length(client, fake, text):
    setup(client)
    r = client.post("/api/feedback/refine", json={"text": text}, headers=login())
    assert r.status_code == 400 and not fake.requests


def test_create_issue(client, fake):
    setup(client)
    r = client.post("/api/feedback", json={"title": "Add a ruler", "body": "Please.", "label": "enhancement"},
                    headers=login())
    assert r.status_code == 200
    assert r.json() == {"number": 7, "url": "https://github.com/htilly/stl-mcp/issues/7"}
    [call] = fake.sent("api.github.com")
    assert str(call.url) == "https://api.github.com/repos/htilly/stl-mcp/issues"
    assert call.headers["authorization"] == "Bearer ghp_secret_5678"
    sent = json.loads(call.content)
    assert sent["title"] == "Add a ruler" and sent["labels"] == ["enhancement"]
    assert sent["body"].startswith("Please.") and "joe@example.com" not in sent["body"]


def test_create_issue_rejects_unknown_label_and_empty_title(client, fake):
    setup(client)
    c = login()
    assert client.post("/api/feedback", json={"title": "T", "label": "evil"}, headers=c).status_code == 400
    assert client.post("/api/feedback", json={"title": " ", "body": "b"}, headers=c).status_code == 400
    assert not fake.requests


@pytest.mark.parametrize("status", [401, 403, 404, 422, 500])
def test_github_errors_become_readable_messages(client, fake, status):
    setup(client)
    fake.github = status
    r = client.post("/api/feedback", json={"title": "T", "body": "b"}, headers=login())
    assert r.status_code == 400
    assert "internal error" not in r.json()["error"] and "ghp_" not in r.text


def test_per_user_limits(client, fake):
    setup(client, per_hour=1)
    c = login()
    assert client.post("/api/feedback", json={"title": "T", "body": "b"}, headers=c).status_code == 200
    assert client.post("/api/feedback", json={"title": "T", "body": "b"}, headers=c).status_code == 429
    ann = login("Ann")
    assert client.post("/api/feedback", json={"title": "T", "body": "b"}, headers=ann).status_code == 200
    for _ in range(3):
        assert client.post("/api/feedback/refine", json={"text": "some words"}, headers=c).status_code == 200
    assert client.post("/api/feedback/refine", json={"text": "some words"}, headers=c).status_code == 429


def test_personal_tokens_cannot_use_it(client, fake):
    setup(client)
    login()
    joe = server.db.one("SELECT id FROM users WHERE lower(email) = 'joe@example.com'")
    raw, _ = server.oauth.state.create_personal_token(joe["id"], "cli")
    r = client.post("/api/feedback/refine", json={"text": "some words"}, headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code in (401, 403) and not fake.requests


# ---- pages

def test_admin_page_is_an_admin_tab(client):
    assert client.get("/admin/feedback").status_code == 401
    assert client.get("/admin/feedback", headers=login()).status_code == 403
    page = client.get("/admin/feedback", headers=ADMIN)
    assert page.status_code == 200 and "/api/admin/feedback" in page.text
    assert 'href="/admin/feedback" aria-current="page"' in page.text
    for other in ("/admin", "/admin/users", "/admin/load", "/admin/design"):
        assert 'href="/admin/feedback"' in client.get(other, headers=ADMIN).text
