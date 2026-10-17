import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://localhost:8765/mcp", headers={"Authorization": "Bearer testtoken"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools])
            res = await s.call_tool("create_enclosure", {
                "name": "test lada", "inner_x": 80, "inner_y": 50, "inner_z": 20,
                "standoffs": [{"x": 10, "y": 10, "hole_d": 2.2}],
                "cutouts": [{"wall": "left", "pos": 25, "width": 12, "height": 8, "z": 4}],
                "lid_holes": [{"x": 40, "y": 25, "d": 4}]})
            print(res.content[0].text[:600])
            res = await s.call_tool("create_primitive", {"name": "ror", "shape": "tube", "diameter": 20, "inner_diameter": 16, "height": 10})
            print(res.content[0].text[:400])
            res = await s.call_tool("create_from_script", {"name": "script", "code": "result = g.diff(g.blk(20,20,10,0,0,0), [g.cyl(5,12,10,10,-1)])"})
            print(res.content[0].text[:400])
            res = await s.call_tool("modify_file", {"file": "ror.stl", "name": "ror_hal",
                "subtract": [{"shape": "cylinder", "diameter": 3, "height": 30, "at": [0, 0, 5], "rotate": [90, 0, 0]}]})
            print(res.content[0].text[:400])
            res = await s.call_tool("export_obj", {"name": "set", "parts": [
                {"file": "test_lada_botten.stl", "color": "blue"}, {"file": "test_lada_lock.stl", "color": "red"}]})
            print(res.content[0].text[:400])
            res = await s.call_tool("list_files", {})
            print([json.loads(c.text)["file"] for c in res.content])
asyncio.run(main())
