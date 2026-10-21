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
    ("/static/fonts/plus-jakarta-sans-latin-wght-normal.woff2", "font/woff2"),
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


def test_static_assets_revalidate(client):
    # not fingerprinted: a CDN must not keep serving the previous deploy's CSS
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers.get("etag")


THEMED_PAGES = sorted(WEB.glob("*.html"))


@pytest.mark.parametrize("page", THEMED_PAGES, ids=lambda p: p.name)
def test_every_page_loads_the_theme_before_it_paints(page):
    """theme.js is a classic script in <head>, right after the stylesheet: no flash of the wrong theme."""
    text = page.read_text()
    head = text[:text.index("</head>")]
    assert '<link rel="stylesheet" href="/static/style.css">\n<script src="/static/theme.js"></script>' in head


def test_theme_script_offers_light_dark_and_auto():
    js = (WEB / "theme.js").read_text()
    for choice in ('"light"', '"auto"', '"dark"'):
        assert f"[{choice}," in js
    # auto means no data-theme, so the prefers-color-scheme block in style.css applies
    assert 'if (t === "auto") delete root.dataset.theme' in js
    assert 'new CustomEvent("themechange"' in js
    assert 'localStorage.setItem(KEY, t)' in js


def test_3d_view_follows_the_theme_switch():
    assert 'addEventListener("themechange"' in (WEB / "stage.js").read_text()


@pytest.mark.parametrize("name", ["index", "gallery", "viewer", "editor", "admin", "users", "design"])
def test_app_pages_have_the_user_menu(name):
    text = (WEB / f"{name}.html").read_text()
    assert "<div data-usermenu></div>" in text and '<script type="module" src="/static/nav.js"></script>' in text


@pytest.mark.parametrize("name", ["login", "consent", "privacy", "demo"])
def test_gate_pages_have_a_theme_switch(name):
    assert '<div class="gate-theme" data-theme-switch></div>' in (WEB / f"{name}.html").read_text()


def test_user_menu_links_into_the_settings_panes():
    """The menu links to /#account, /#security and /#connect; the library opens that pane."""
    nav = (WEB / "nav.js").read_text()
    index = (WEB / "index.html").read_text()
    for pane in ("account", "security", "connect"):
        assert f'"/#{pane}"' in nav
        assert f'data-pane="{pane}"' in index
    assert 'const PANES = ["account", "security", "connect"];' in index


def test_nav_icons_exist():
    used = set(re.findall(r'icon\("([a-z-]+)"', (WEB / "nav.js").read_text()))
    used |= set(re.findall(r'item\("[^"]*", "([a-z-]+)"', (WEB / "nav.js").read_text()))
    used |= set(re.findall(r'\["(?:light|auto|dark)", "([a-z-]+)"', (WEB / "theme.js").read_text()))
    assert used and {f"i-{n}" for n in used} <= SYMBOLS


def test_touch_screens_get_finger_sized_targets_and_no_input_zoom():
    css = (WEB / "style.css").read_text()
    touch = re.search(r"@media \(pointer: coarse\) \{(.*?)\n\}", css, re.S).group(1)
    assert "button, .btn { height: 40px; }" in touch
    assert ".menu a, .menu button { height: 44px;" in touch
    # iOS zooms the page into any focused input under 16 px
    assert re.search(r"input\[type=search\].*?\{ font-size: 16px; \}", touch)


def test_phone_layout_keeps_toolbars_off_the_model_and_settings_full_screen():
    css = (WEB / "style.css").read_text()
    phone = css[css.index("@media (max-width: 860px) {"):]
    assert ".toolbar {\n    flex-wrap: nowrap; overflow-x: auto;" in phone
    assert "dialog.settings-dlg {" in phone and "height: 100dvh;" in phone
    editor = (WEB / "editor.html").read_text()
    assert ".ed-tools { flex-wrap: nowrap; overflow-x: auto;" in editor and "#stage { order: -1; }" in editor


def test_viewer_and_editor_headers_have_a_back_link_that_shows_only_on_phones():
    """The breadcrumb is hidden on phones, so the header needs an explicit way back to the library."""
    css = (WEB / "style.css").read_text()
    assert "header.top .back { display: none; }" in css
    phone = css[css.index("@media (max-width: 520px) {"):]
    assert "header.top .back { display: inline-flex; }" in phone
    for page in ("viewer.html", "editor.html"):
        html = (WEB / page).read_text()
        header = html[html.index('<header class="top">'):html.index("</header>")]
        assert 'class="btn icon back" href="/"' in header and "#i-back" in header
        assert header.index('class="btn icon back"') < header.index('class="wordmark"')


def test_settings_tabs_have_short_labels_for_phones():
    index = (WEB / "index.html").read_text()
    for short in ("Profile", "Security", "AI apps"):
        assert f'<span class="short">{short}</span>' in index


def test_only_the_login_page_links_to_the_demo_at_the_bottom():
    """A link to /demo (play icon and text) only on the login page, as the last thing in the panel."""
    login = (WEB / "login.html").read_text()
    assert login.count('<a class="demo-link" href="/demo">') == 1
    assert login.index('class="demo-link"') > login.index('id="tok"')
    assert "demo-poster" not in login and "<video" not in login
    assert "demo-link" not in (WEB / "index.html").read_text()


def test_viewer_toolbar_puts_comment_first_and_measure_second():
    """Comment and Measure are the most used controls: they come first, stay labelled on phones,
    and camera/display controls are demoted after them."""
    html = (WEB / "viewer.html").read_text()
    bar = html[html.index('<div class="toolbar">'):html.index('<div id="pins">')]
    order = [bar.index(m) for m in ('id="commentmode"', 'id="measuremode"', 'aria-label="Camera"', 'aria-label="Display"')]
    assert order == sorted(order)
    assert 'class="cta"' in bar and 'id="cbadge"' in bar
    css = (WEB / "style.css").read_text()
    assert ".toolbar .cta {" in css
    assert ".toolbar .label:not(.keep) { display: none; }" in css
    assert "badge" in (WEB / "comments.js").read_text()
