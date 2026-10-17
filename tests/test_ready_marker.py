import asyncio
import socket

import uvicorn

import server


def test_ready_line_includes_sha_and_port(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "0123456789abcdef0123456789abcdef01234567")
    assert server.ready_line(8765) == (
        "STL_MCP_READY sha=0123456789abcdef0123456789abcdef01234567 port=8765")


def test_ready_line_without_sha_says_unknown(monkeypatch):
    monkeypatch.delenv("GIT_SHA", raising=False)
    assert server.ready_line(8765) == "STL_MCP_READY sha=unknown port=8765"
    monkeypatch.setenv("GIT_SHA", "")
    assert server.ready_line(8765) == "STL_MCP_READY sha=unknown port=8765"


def test_marker_matches_deploy_workflow():
    # deploy.yml greps for this exact string; a rename on one side only must fail here.
    workflow = (server.Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "deploy.yml").read_text()
    assert f"READY_MARKER: {server.READY_MARKER}\n" in workflow


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _noop_app(scope, receive, send):
    if scope["type"] == "lifespan":
        while (await receive())["type"] != "lifespan.shutdown":
            await send({"type": "lifespan.startup.complete"})
        await send({"type": "lifespan.shutdown.complete"})


def test_marker_is_printed_once_the_port_is_listening(monkeypatch, capsys):
    port = _free_port()
    listening_at_print = []

    def probe(p):
        # Called at the moment the marker is printed: the port must already accept connections.
        with socket.socket() as s:
            listening_at_print.append(s.connect_ex(("127.0.0.1", p)) == 0)
        return f"{server.READY_MARKER} sha=test port={p}"

    monkeypatch.setattr(server, "ready_line", probe)
    srv = server.Server(uvicorn.Config(_noop_app, host="127.0.0.1", port=port, log_level="warning"))

    async def run():
        task = asyncio.create_task(srv.serve())
        for _ in range(100):
            if srv.started:
                break
            await asyncio.sleep(0.05)
        srv.should_exit = True
        await task

    asyncio.run(run())
    assert listening_at_print == [True]
    assert f"STL_MCP_READY sha=test port={port}" in capsys.readouterr().out
