"""Snabbtest mot en körande server: MCP_TOKEN=testtoken python server.py & python test_client.py"""
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
        sys.exit(f"FEL: {res.content[0].text}")
    return json.loads(res.content[0].text)


async def main():
    async with streamablehttp_client(f"{URL}/mcp", headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("verktyg:", [t.name for t in (await s.list_tools()).tools])

            m = out(await s.call_tool("create_model", {"name": "Hållare för penna", "spec": {
                "vars": {"d": 12, "wall": 2, "h": 40, "n": 3},
                "root": {"op": "difference", "children": [
                    {"shape": "box", "size": ["=n*(d+wall)+wall", "=d+2*wall", "=h"], "radius": 3,
                     "round": "vertical"},
                    {"shape": "cylinder", "d": "=d", "h": "=h",
                     "translate": ["=wall+d/2", "=wall+d/2", "=wall"],
                     "array": {"count": ["=n", 1, 1], "step": ["=d+wall", 0, 0]}},
                ]}}}))
            print("skapad:", m["model_id"], m["viewer_url"], m["parts"]["model"]["extents_mm"])

            u = out(await s.call_tool("update_model", {"model_id": m["model_id"],
                                                       "changes": {"n": 4, "h": 50}, "note": "4 hål, högre"}))
            print("v%d:" % u["version"], u["parts"]["model"]["extents_mm"])

            e = out(await s.call_tool("create_enclosure", {
                "name": "test låda", "inner_x": 80, "inner_y": 50, "inner_z": 20,
                "standoffs": [{"x": 10, "y": 10, "hole_d": 2.2}],
                "cutouts": [{"wall": "left", "pos": 25, "width": 12, "height": 8, "z": 4}],
                "lid_holes": [{"x": 40, "y": 25, "d": 4}]}))
            e = out(await s.call_tool("update_model", {"model_id": e["model_id"],
                                                       "changes": {"inner_z": 30, "/cutouts/0/width": 14}}))
            print("låda:", e["model_id"], "v%d" % e["version"], {k: v["extents_mm"] for k, v in e["parts"].items()})

            g = out(await s.call_tool("get_model", {"model_id": m["model_id"]}))
            print("historik:", g["history"])

            r = await s.call_tool("render_model", {"model_id": e["model_id"]})
            img = [c for c in r.content if c.type == "image"][0]
            open("render_test.png", "wb").write(base64.b64decode(img.data))
            print("render -> render_test.png")

            r = await s.call_tool("update_model", {"model_id": m["model_id"], "changes": {"/root/op": "nope"}})
            print("felhantering:", r.isError, r.content[0].text[:80])
            # mallar + kommentarer
            p = out(await s.call_tool("create_model", {"name": "Platta med skruvar", "spec": {
                "vars": {"t": 4},
                "root": {"op": "difference", "children": [
                    {"shape": "box", "size": [60, 30, "=t"], "radius": 3, "round": "vertical"},
                    {"name": "skruvhål", "shape": "screw_hole", "size": "M3", "head": "countersunk",
                     "h": "=t", "translate": [8, 15, "=t"], "array": {"count": [2, 1, 1], "step": [44, 0, 0]}},
                    {"shape": "text", "text": "OK", "size": 8, "h": 1, "halign": "center",
                     "valign": "center", "translate": [30, 15, "=t-0.6"]}]}}}))
            print("mallar:", p["model_id"], p["parts"]["model"]["extents_mm"], p["parts"]["model"]["watertight"])
            async with httpx.AsyncClient(base_url=URL, headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                r = await http.post(f"/api/models/{p['model_id']}", json={
                    "action": "comment_add", "text": "hålet här ska vara 2 mm större",
                    "point": [8.2, 15.1, 4.0], "part": "model"})
                assert r.status_code == 200, r.text
            g = out(await s.call_tool("get_model", {"model_id": p["model_id"]}))
            c = g["comments"][0]
            print("kommentar:", c["id"], c["text"], "→", c["near_nodes"][0])
            assert c["near_nodes"][0]["pointer"] == "/root/children/1", c["near_nodes"]
            u = out(await s.call_tool("update_model", {"model_id": p["model_id"],
                                                       "changes": {"/root/children/1/size": "M4"},
                                                       "note": "M4-hål", "resolves": [c["id"]]}))
            g = out(await s.call_tool("get_model", {"model_id": p["model_id"]}))
            print("efter åtgärd: öppna", len(g["comments"]), "lösta", g["resolved_comments"], "v", u["version"])
            res = await s.call_tool("list_models", {})
            print("lista:", [json.loads(c.text)["model_id"] for c in res.content])

asyncio.run(main())
