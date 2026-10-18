"""The design system assets (docs/DESIGN.md): served, consistent, and in step with the renderer."""
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import render
import server

WEB = Path(server.__file__).parent / "web"
SPRITE = (WEB / "icons.svg").read_text()
SYMBOLS = set(re.findall(r'<symbol id="(i-[a-z-]+)"', SPRITE))
PAGES = sorted(p for p in WEB.glob("*.html")) + [WEB / "common.js"]


@pytest.fixture
def client(out_dir, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "")
    return TestClient(server.app, follow_redirects=False)


@pytest.mark.parametrize("path,ctype", [
    ("/static/icons.svg", "image/svg+xml"),
    ("/static/favicon.svg", "image/svg+xml"),
    ("/static/fonts/archivo-latin-wdth-normal.woff2", "font/woff2"),
    ("/static/fonts/ibm-plex-mono-latin-400-normal.woff2", "font/woff2"),
    ("/static/fonts/ibm-plex-mono-latin-500-normal.woff2", "font/woff2"),
])
def test_assets_are_served(client, path, ctype):
    r = client.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith(ctype)


def test_every_referenced_icon_exists():
    used = set()
    for p in PAGES:
        text = p.read_text()
        used |= set(re.findall(r"icons\.svg#(i-[a-z-]+)", text))
        used |= {f"i-{n}" for n in re.findall(r'icon\("([a-z-]+)"', text)}
    used |= {f"i-{n}" for n in re.findall(r'"([a-z-]+)"', re.search(
        r"const ICONS = \[(.*?)\];", (WEB / "design.html").read_text(), re.S).group(1))}
    used.discard("i-name")  # the placeholder in design.html's usage example
    assert used and used <= SYMBOLS, f"missing in icons.svg: {sorted(used - SYMBOLS)}"


def test_every_icon_is_documented():
    doc = (Path(server.__file__).parent / "docs" / "DESIGN.md").read_text()
    undocumented = {s for s in SYMBOLS if f"`{s}`" not in doc}
    assert not undocumented


def test_part_colors_match_renderer():
    """The 3D view and the preview Claude sees must colour parts the same way."""
    js = (WEB / "common.js").read_text()
    hexes = re.search(r"PART_COLORS = \[(.*?)\]", js).group(1)
    web = [tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) for h in re.findall(r"#([0-9a-f]{6})", hexes)]
    assert web == [tuple(c) for c in render.PART_COLORS]


def test_preview_plate_matches_renderer():
    css = (WEB / "style.css").read_text()
    plate = re.search(r"--plate: #([0-9a-f]{6})", css).group(1)
    assert tuple(int(plate[i:i + 2], 16) for i in (0, 2, 4)) == render.BG


def test_tokens_have_dark_values():
    """Every colour that changes in dark mode is redefined in both dark blocks (auto and forced)."""
    css = (WEB / "style.css").read_text()
    auto = re.search(r':root:not\(\[data-theme="light"\]\) \{(.*?)\}', css, re.S).group(1)
    forced = re.search(r':root\[data-theme="dark"\] \{(.*?)\}', css, re.S).group(1)
    names = lambda block: set(re.findall(r"(--[a-z-]+):", block))
    assert names(auto) == names(forced) and "--stage" in names(auto)


def test_design_page_is_in_admin(client):
    r = client.get("/admin/design")
    assert r.status_code == 200 and "Design system" in r.text
    for section in ("colour", "text-styles", "fonts", "icons"):
        assert f'id="{section}"' in r.text
    assert 'href="/admin/design" aria-current="page"' in r.text


def test_admin_pages_link_to_each_other():
    assert 'href="/admin/design"' in (WEB / "admin.html").read_text()
    assert 'href="/admin"' in (WEB / "design.html").read_text()


def test_design_page_requires_login_even_when_ui_is_public(client, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "secret")
    monkeypatch.setattr(server, "UI_PUBLIC", True)
    assert client.get("/admin/design").status_code == 401
    assert client.get("/admin/design", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_text_styles_are_defined_and_documented():
    css = (WEB / "style.css").read_text()
    doc = (Path(server.__file__).parent / "docs" / "DESIGN.md").read_text()
    used = set(re.findall(r'\["(ts-[a-z]+)"', (WEB / "design.html").read_text()))
    assert len(used) == 9
    for cls in used:
        assert re.search(rf"\.{cls}\s*\{{", css) and f"`.{cls}`" in doc
