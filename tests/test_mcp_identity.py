"""MCP tool calls run for the user behind the HTTP request, over the real streamable HTTP transport.

The MCP session manager can only be started once per process, so everything that needs the full
app lifespan lives in this one test."""
import json

from starlette.testclient import TestClient

import server

ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(client, headers, method, params=None, id=1):
    body = {"jsonrpc": "2.0", "method": method, **({"id": id} if id is not None else {}),
            **({"params": params} if params is not None else {})}
    r = client.post("/mcp", json=body, headers={**ACCEPT, **headers})
    assert r.status_code in (200, 202), r.text
    if id is None:
        return r, None
    data = next(line[5:] for line in r.text.splitlines() if line.startswith("data:"))
    return r, json.loads(data)


def _session(client, token):
    h = {"Authorization": f"Bearer {token}"}
    r, _ = _rpc(client, h, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "1"}})
    h["mcp-session-id"] = r.headers["mcp-session-id"]
    _rpc(client, h, "notifications/initialized", id=None)
    return h


def _whoami(client, h):
    _, msg = _rpc(client, h, "tools/call", {"name": "whoami", "arguments": {}}, id=2)
    result = msg["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_tool_calls_see_the_calling_user(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    st = server.oauth.state
    joe = st.create_user("Joe", "joe@example.com")
    ann = st.create_user("Ann", "ann@example.com")
    joe_tok, _ = st.create_personal_token(joe["id"], "cli")
    ann_tok, _ = st.create_personal_token(ann["id"], "cli")

    with TestClient(server.app) as client:  # runs the lifespan: migrations + MCP session manager
        joe_s, ann_s = _session(client, joe_tok), _session(client, ann_tok)
        # interleaved calls on two live sessions each see their own user
        assert _whoami(client, joe_s)["email"] == "joe@example.com"
        assert _whoami(client, ann_s)["email"] == "ann@example.com"
        assert _whoami(client, joe_s)["email"] == "joe@example.com"
        assert _whoami(client, _session(client, "secret"))["role"] == "admin"
        # without a valid token there is no session at all
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={**ACCEPT, "Authorization": "Bearer nope"})
        assert r.status_code == 401
