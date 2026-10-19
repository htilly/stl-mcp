"""Reference material for the AI: lookup_part, print_guide and their MCP resources."""
import asyncio
import json

import reference
import server


def run(coro):
    return asyncio.run(coro)


def test_parts_are_well_formed():
    for key, p in reference.parts().items():
        assert p["name"] and len(p["size"]) == 3 and all(v > 0 for v in p["size"][:2]), key
        for x, y in p.get("holes", []):
            assert 0 < x < p["size"][0] and 0 < y < p["size"][1], key


def test_lookup_part():
    assert [p["key"] for p in server.lookup_part("esp32")["parts"]][0] == "esp32-devkit"
    assert [p["key"] for p in server.lookup_part("s3 mini")["parts"]] == ["lolin-s2-mini"]
    assert server.lookup_part("feather esp32-s3")["parts"][0]["holes"][2] == [48.26, 1.84]
    pi4 = server.lookup_part("Pi 4")["parts"]
    assert [p["key"] for p in pi4] == ["raspberry-pi-4"]
    assert pi4[0]["holes"] == [[3.5, 3.5], [61.5, 3.5], [3.5, 52.5], [61.5, 52.5]]
    assert pi4[0]["nopscadlib"] == "RPI4"
    assert len(server.lookup_part()["parts"]) == len(reference.parts())
    miss = server.lookup_part("flux capacitor")
    assert miss["parts"] == [] and "Raspberry Pi 4 Model B" in miss["available"]


def test_print_guide():
    assert "Heat-set inserts" in server.print_guide()


def test_resources():
    uris = {str(r.uri) for r in run(server.mcp.list_resources())}
    assert "autosmith://guides/printing" in uris
    templates = {t.uriTemplate for t in run(server.mcp.list_resource_templates())}
    assert "autosmith://parts/{key}" in templates
    part = run(server.mcp.read_resource("autosmith://parts/raspberry-pi-pico"))
    assert json.loads(list(part)[0].content)["size"] == [51, 21, 1.6]
