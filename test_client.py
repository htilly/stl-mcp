"""Quick test against a running server: MCP_TOKEN=testtoken python server.py & python test_client.py"""
import asyncio
import base64
import json
import os
import sys

import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("URL", "http://localhost:8765")
TOKEN = os.environ.get("MCP_TOKEN", "testtoken")


def out(res):
    if res.isError:
        sys.exit(f"ERROR: {res.content[0].text}")
    return json.loads(res.content[0].text)


async def main():
    async with streamablehttp_client(f"{URL}/mcp", headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools])

            m = out(await s.call_tool("create_model", {"name": "Pen holder", "spec": {
                "vars": {"d": 12, "wall": 2, "h": 40, "n": 3},
                "root": {"op": "difference", "children": [
                    {"shape": "box", "size": ["=n*(d+wall)+wall", "=d+2*wall", "=h"], "radius": 3,
                     "round": "vertical"},
                    {"shape": "cylinder", "d": "=d", "h": "=h",
                     "translate": ["=wall+d/2", "=wall+d/2", "=wall"],
                     "array": {"count": ["=n", 1, 1], "step": ["=d+wall", 0, 0]}},
                ]}}}))
            print("created:", m["model_id"], m["viewer_url"], m["parts"]["model"]["extents_mm"])

            u = out(await s.call_tool("update_model", {"model_id": m["model_id"],
                                                       "changes": {"n": 4, "h": 50}, "note": "4 holes, taller"}))
            print("v%d:" % u["version"], u["parts"]["model"]["extents_mm"])

            e = out(await s.call_tool("create_enclosure", {
                "name": "test box", "inner_x": 80, "inner_y": 50, "inner_z": 20,
                "standoffs": [{"x": 10, "y": 10, "hole_d": 2.2}],
                "cutouts": [{"wall": "left", "pos": 25, "width": 12, "height": 8, "z": 4}],
                "lid_holes": [{"x": 40, "y": 25, "d": 4}]}))
            e = out(await s.call_tool("update_model", {"model_id": e["model_id"],
                                                       "changes": {"inner_z": 30, "/cutouts/0/width": 14}}))
            print("box:", e["model_id"], "v%d" % e["version"], {k: v["extents_mm"] for k, v in e["parts"].items()})

            g = out(await s.call_tool("get_model", {"model_id": m["model_id"]}))
            print("history:", g["history"])

            r = await s.call_tool("render_model", {"model_id": e["model_id"]})
            img = [c for c in r.content if c.type == "image"][0]
            open("render_test.png", "wb").write(base64.b64decode(img.data))
            print("render -> render_test.png")

            r = await s.call_tool("update_model", {"model_id": m["model_id"], "changes": {"/root/op": "nope"}})
            print("error handling:", r.isError, r.content[0].text[:80])
            # templates + comments
            p = out(await s.call_tool("create_model", {"name": "Plate with screws", "spec": {
                "vars": {"t": 4},
                "root": {"op": "difference", "children": [
                    {"shape": "box", "size": [60, 30, "=t"], "radius": 3, "round": "vertical"},
                    {"name": "screw hole", "shape": "screw_hole", "size": "M3", "head": "countersunk",
                     "h": "=t", "translate": [8, 15, "=t"], "array": {"count": [2, 1, 1], "step": [44, 0, 0]}},
                    {"shape": "text", "text": "OK", "size": 8, "h": 1, "halign": "center",
                     "valign": "center", "translate": [30, 15, "=t-0.6"]}]}}}))
            print("templates:", p["model_id"], p["parts"]["model"]["extents_mm"], p["parts"]["model"]["watertight"])
            async with httpx.AsyncClient(base_url=URL, headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                r = await http.post(f"/api/models/{p['model_id']}", json={
                    "action": "comment_add", "text": "the hole here should be 2 mm larger",
                    "point": [8.2, 15.1, 4.0], "part": "model"})
                assert r.status_code == 200, r.text
            g = out(await s.call_tool("get_model", {"model_id": p["model_id"]}))
            c = g["comments"][0]
            print("comment:", c["id"], c["text"], "→", c["near_nodes"][0])
            assert c["near_nodes"][0]["pointer"] == "/root/children/1", c["near_nodes"]
            u = out(await s.call_tool("update_model", {"model_id": p["model_id"],
                                                       "changes": {"/root/children/1/size": "M4"},
                                                       "note": "M4 hole", "resolves": [c["id"]]}))
            g = out(await s.call_tool("get_model", {"model_id": p["model_id"]}))
            print("after fix: open", len(g["comments"]), "resolved", g["resolved_comments"], "v", u["version"])
            # standalone files (Tinkercad): import, modify, export of library parts
            async with httpx.AsyncClient(base_url=URL, headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                stl = await http.get(u["parts"]["model"]["url"].removeprefix(URL))
                r = await http.put("/upload/import_test.stl", content=stl.content)
                assert r.status_code == 200, r.text
            f = out(await s.call_tool("modify_file", {"file": "import_test.stl", "name": "import_hole",
                "subtract": [{"shape": "cylinder", "diameter": 3, "height": 30, "at": [10, 10, 5]}]}))
            print("modify_file:", f["file"], f["watertight"])
            rel = lambda p: "/".join(p["url"].split("/files/")[1].split("/"))
            x = out(await s.call_tool("export_obj", {"name": "set", "parts": [
                {"file": rel(e["parts"]["body"]), "color": "blue"},
                {"file": rel(e["parts"]["lid"]), "color": "red"}]}))
            print("export_obj:", x["parts"], x["triangles"])
            res = await s.call_tool("list_files", {})
            print("files:", [json.loads(c.text)["file"] for c in res.content])
            res = await s.call_tool("list_models", {})
            print("list:", [json.loads(c.text)["model_id"] for c in res.content])

asyncio.run(main())
