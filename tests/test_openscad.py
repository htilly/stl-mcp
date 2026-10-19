"""OpenSCAD models: the source check always runs; builds skip when openscad is not installed."""
import asyncio

import pytest

import openscad
import server
from openscad import OpenSCADError, check

needs_openscad = pytest.mark.skipif(not openscad.available(), reason="openscad is not installed")
needs_bosl2 = pytest.mark.skipif(not (openscad.LIBS / "BOSL2" / "std.scad").is_file(),
                                 reason="BOSL2 is not installed")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def scad(out_dir):
    server.store.allow_openscad = True
    return out_dir


# ---- source check (no binary needed)

@pytest.mark.parametrize("code", [
    'import("/etc/passwd");',
    'import ( file = "x.stl" );',
    'surface(file="/etc/hostname");',
    'x = dxf_dim(file="a.dxf", name="w");',
    "include </etc/passwd>",
    "use</etc/passwd>",
    "include <../../etc/passwd>",
    "include <BOSL2/../../../etc/passwd.scad>",
    "include <x.scad>",
    "include <BOSL2/std.scad",
    "cube(1); /* never closed",
    'echo("never closed);',
    "include\n>\n</etc/passwd>",
])
def test_check_rejects_file_access(code):
    with pytest.raises(OpenSCADError):
        check(code)


@pytest.mark.parametrize("code", [
    'cube(10); // import("/etc/passwd")',
    '/* include </etc/passwd> */ cube(1);',
    'echo("import(\\"/etc/passwd\\") and include </etc/passwd>"); cube(1);',
    "my_import = 1; important = 2; cube([1e3, 2.5e-1, .5]);",
    "$fn = 32; cylinder(d=3, h=2);",
])
def test_check_ignores_strings_and_comments(code):
    assert check(code) == []


def test_check_rejects_library_wrappers_of_file_readers(tmp_path, monkeypatch):
    """A library module that calls a file reader (BOSL2's builtins.scad `_surface(file)`) is as
    forbidden as the reader itself, also through further wrappers and function literals."""
    lib = tmp_path / "BOSL2"
    lib.mkdir()
    (lib / "builtins.scad").write_text(
        "// surface(file) in a comment is not a call\n"
        "module _surface(file,center,convexity,invert)\n"
        "  if ($children>0) cube(1) children();\n"
        "  else surface(file=file,center=center,convexity=convexity,invert=invert);\n"
        "module wrap(f) { translate([1, 0, 0]) _surface(f); }\n"
        "function reader(f) = dxf_dim(file=f, name=\"w\");\n"
        "via = function(f) reader(f);\n"
        "module fine(surface=1) cube(surface);\n"
        "function also_fine(x) = x * 2;\n")
    monkeypatch.setattr(openscad, "LIBS", tmp_path)
    assert openscad._forbidden(tmp_path) == {
        "import", "surface", "dxf_dim", "dxf_cross", "_surface", "wrap", "reader", "via"}
    for code in ('use <BOSL2/builtins.scad>\n_surface(file="/etc/hostname");',
                 'use <BOSL2/builtins.scad> wrap("/etc/hostname");',
                 'include <BOSL2/builtins.scad> x = via("a.dxf");'):
        with pytest.raises(OpenSCADError, match="not allowed"):
            check(code)
    assert check("use <BOSL2/builtins.scad>\nfine(); echo(also_fine(2));") == ["BOSL2/builtins.scad"]


def _lib(root, name, files):
    (root / name).mkdir(parents=True)
    for f, src in files.items():
        (root / name / f).write_text(src)


def test_file_reader_wrappers_are_blocked_per_library(tmp_path, monkeypatch):
    """A name that one library wraps around a file reader does not block the same name in
    another library, unless the code includes the first one too, also through a library that
    includes it (by library path or relative path)."""
    _lib(tmp_path, "reader", {"lib.scad": "module part(f) import(f);\n"})
    _lib(tmp_path, "plain", {"lib.scad": "module part(s) cube(s);\n"})
    _lib(tmp_path, "wrapper", {"lib.scad": "use <reader/lib.scad>\nmodule other() cube(1);\n"})
    _lib(tmp_path, "relative", {"lib.scad": "include <../reader/lib.scad>\n"})
    monkeypatch.setattr(openscad, "LIBS", tmp_path)
    assert openscad.libraries() == ["plain", "reader", "relative", "wrapper"]
    assert check("use <plain/lib.scad>\npart(2);") == ["plain/lib.scad"]
    for code in ("use <reader/lib.scad>\npart(\"/etc/passwd\");",
                 "part(\"/etc/passwd\"); use <plain/lib.scad> use <reader/lib.scad>",
                 "include <wrapper/lib.scad>\npart(\"/etc/passwd\");",
                 "include <relative/lib.scad>\npart(\"/etc/passwd\");"):
        with pytest.raises(OpenSCADError, match="part\\(\\) is not allowed"):
            check(code)
    with pytest.raises(OpenSCADError, match="only the bundled libraries.*bundled: plain, reader"):
        check("include <elsewhere/lib.scad>")
    with pytest.raises(OpenSCADError, match="no such library file"):
        check("include <plain/missing.scad>")


@needs_bosl2
def test_bundled_bosl2_surface_wrapper_is_blocked():
    with pytest.raises(OpenSCADError, match="_surface"):
        check('use <BOSL2/builtins.scad>\n_surface(file="/etc/hostname");')


def test_builds_are_limited_server_wide(monkeypatch):
    """No more than CONCURRENCY builds at once: one that cannot start in time fails as busy."""
    monkeypatch.setattr(openscad, "available", lambda: True)
    monkeypatch.setattr(openscad, "TIMEOUT", 0.05)
    monkeypatch.setattr(openscad, "_slots", openscad.Slots(1))
    monkeypatch.setattr(openscad, "_build", lambda *a: {"model": "built"})
    assert openscad.build({"code": "cube(1);"}) == {"model": "built"}   # the slot is given back
    openscad._slots.acquire()
    try:
        with pytest.raises(OpenSCADError, match="busy"):
            openscad.build({"code": "cube(1);"})
    finally:
        openscad._slots.release()


def test_backend_is_passed_only_to_binaries_that_have_it(monkeypatch):
    """Manifold by default on a nightly; 2021.01 has no --backend and gets no such option."""
    monkeypatch.setattr(openscad, "BACKEND", "manifold")
    monkeypatch.setattr(openscad, "_has_backend_option", lambda b: True)
    assert openscad._backend_args() == ["--backend=manifold"]
    monkeypatch.setattr(openscad, "_has_backend_option", lambda b: False)
    assert openscad._backend_args() == []
    monkeypatch.setattr(openscad, "_has_backend_option", lambda b: True)
    monkeypatch.setattr(openscad, "BACKEND", "")
    assert openscad._backend_args() == []


def test_resource_limits_apply_where_supported(monkeypatch):
    """The rlimits are not Linux-only any more; a limit the OS rejects is skipped only off Linux."""
    import resource
    calls = []

    def setrlimit(lim, val):
        calls.append(lim)
        if lim == resource.RLIMIT_AS:
            raise ValueError("not supported")
    monkeypatch.setattr(resource, "setrlimit", setrlimit)
    monkeypatch.setattr(openscad.sys, "platform", "darwin")
    openscad._limits(10)()
    assert resource.RLIMIT_CPU in calls and resource.RLIMIT_FSIZE in calls
    monkeypatch.setattr(openscad.sys, "platform", "linux")
    with pytest.raises(ValueError):
        openscad._limits(10)()


@needs_bosl2
def test_check_allows_bosl2():
    assert check("include <BOSL2/std.scad>\nuse<BOSL2/threading.scad>") == [
        "BOSL2/std.scad", "BOSL2/threading.scad"]


def test_bad_variables_are_rejected():
    for v in ({"a; import(\"x\")": 1}, {"PART": "x"}, {"s": "a\nb"}, {"x": {"y": 1}}):
        with pytest.raises(OpenSCADError):
            openscad._overrides(v)
    assert openscad._overrides({"w": 3, "s": 'a"b', "on": True, "v": [1, 2.5]}) == [
        "-D", "w=3", "-D", 's="a\\"b"', "-D", "on=true", "-D", "v=[1,2.5]"]


def test_disabled_by_flag(out_dir):
    server.store.allow_openscad = False
    with pytest.raises(PermissionError):
        run(server.create_from_openscad("c", "cube(10);"))


def test_rejected_code_saves_nothing(scad):
    with pytest.raises(OpenSCADError):
        run(server.create_from_openscad("c", 'import("/etc/passwd");'))
    assert server.list_models() == []


# ---- builds

@needs_openscad
def test_cube(scad):
    s = run(server.create_from_openscad("Cube", "w = 10;\ncube([w, 20, 5]);"))
    assert s["kind"] == "openscad"
    assert s["parts"]["model"]["extents_mm"] == [10.0, 20.0, 5.0]
    assert s["parts"]["model"]["watertight"]


@needs_openscad
def test_variable_override_and_code_replace(scad):
    s = run(server.create_from_openscad("Cube", "w = 10;\ncube([w, 20, 5]);"))
    s = run(server.update_model(s["model_id"], {"w": 30}))
    assert s["version"] == 2 and s["parts"]["model"]["extents_mm"] == [30.0, 20.0, 5.0]
    m = server.get_model(s["model_id"])
    assert m["params"]["vars"] == {"w": 30} and "w = 10" in m["params"]["code"]
    s = run(server.update_model(s["model_id"], {"code": "w = 1;\ncube([w, 2, 3]);"}))
    assert s["parts"]["model"]["extents_mm"] == [30.0, 2.0, 3.0]  # the override still applies


@needs_openscad
def test_parts(scad):
    code = 'if (PART == "body") cube(10); else translate([0, 0, 20]) cube([10, 10, 2]);'
    s = run(server.create_from_openscad("Box", code, parts=["body", "lid"]))
    assert set(s["parts"]) == {"body", "lid"}
    assert s["parts"]["lid"]["extents_mm"] == [10.0, 10.0, 2.0]


@needs_openscad
def test_errors_are_reported(scad):
    with pytest.raises(OpenSCADError, match="no 3D object|empty|ERROR|WARNING"):
        run(server.create_from_openscad("Empty", "x = 1;"))


@needs_openscad
@needs_bosl2
def test_bosl2_threaded_rod(scad):
    code = ("include <BOSL2/std.scad>\ninclude <BOSL2/threading.scad>\n"
            "threaded_rod(d=10, l=20, pitch=1.5, anchor=BOTTOM, $fn=24);")
    s = run(server.create_from_openscad("Rod", code))
    ext = s["parts"]["model"]["extents_mm"]
    assert 9 < ext[0] <= 10.1 and abs(ext[2] - 20) < 0.1


needs_nopscadlib = pytest.mark.skipif(not (openscad.LIBS / "NopSCADlib" / "core.scad").is_file(),
                                      reason="NopSCADlib is not installed")


@needs_nopscadlib
def test_nopscadlib_names_are_not_blocked_by_other_libraries():
    """NopSCADlib's own printed parts load STL files (blocked); its vitamins do not, and its
    wrappers do not block BOSL2's names."""
    assert check("include <NopSCADlib/core.scad>\ninclude <NopSCADlib/vitamins/pcbs.scad>\n"
                 "difference() { cube(10); pcb_cutouts(RPI4); }")
    with pytest.raises(OpenSCADError, match="pcb_assembly"):
        check("include <NopSCADlib/lib.scad>\npcb_assembly(RPI4, 5, 3);")
    assert openscad._library_forbidden(openscad.LIBS, {"BOSL2"}) == openscad._forbidden(openscad.LIBS / "BOSL2")


@needs_openscad
@needs_nopscadlib
def test_nopscadlib_enclosure_cutouts(scad):
    code = ("include <NopSCADlib/core.scad>\ninclude <NopSCADlib/vitamins/pcbs.scad>\n"
            "difference() { translate([-48, -33, -3]) cube([96, 66, 25]);\n"
            "  translate([-44, -29, -1]) cube([88, 58, 30]); pcb_cutouts(RPI4); }")
    s = run(server.create_from_openscad("Pi box", code))
    assert s["parts"]["model"]["extents_mm"] == [96.0, 66.0, 25.0]
    # the walls alone are 96*66*25 - 88*58*23 mm³; the connector openings take some of that
    assert s["parts"]["model"]["volume_cm3"] < (96 * 66 * 25 - 88 * 58 * 23) / 1000 - 0.5


@needs_openscad
def test_timeout(scad, monkeypatch):
    monkeypatch.setattr(openscad, "TIMEOUT", 2)
    slow = "function f(n) = n <= 0 ? 0 : 1 + f(n - 1) + f(n - 1);\nx = f(60);\ncube(1);"
    with pytest.raises(OpenSCADError, match="longer than"):
        run(server.create_from_openscad("Slow", slow))


# ---- attached meshes: import("<name>.stl") of the user's own files

def test_check_allows_import_of_attached_meshes_only():
    meshes = {"base"}
    assert check('import("base.stl");', meshes) == []
    assert check('difference() { import ( file = "base.stl", convexity = 4 ); cube(1); }', meshes) == []
    for code in ('import("other.stl");', 'import("/etc/passwd");', 'import("../base.stl");',
                 'f = "base.stl"; import(f);', 'import(str("base", ".stl"));',
                 'import("base.stl/../../etc/passwd");', 'surface(file="base.stl");'):
        with pytest.raises(OpenSCADError, match="not allowed|only read the attached"):
            check(code, meshes)
    with pytest.raises(OpenSCADError, match="not allowed"):
        check('import("base.stl");')               # nothing attached: no import at all


def test_meshes_must_be_in_the_mesh_dir(tmp_path):
    sha = "a" * 64
    with pytest.raises(OpenSCADError, match="not available"):
        openscad._meshes({"meshes": {"base": sha}}, tmp_path)
    with pytest.raises(OpenSCADError, match="not available"):
        openscad._meshes({"meshes": {"base": "../../etc/passwd"}}, tmp_path)
    with pytest.raises(OpenSCADError, match="invalid mesh name"):
        openscad._meshes({"meshes": {"../x": sha}}, tmp_path)
    (tmp_path / f"{sha}.stl").write_bytes(b"x")
    assert openscad._meshes({"meshes": {"base": sha}}, tmp_path) == {"base": tmp_path / f"{sha}.stl"}


def ctx(user):
    from types import SimpleNamespace
    return SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(user=user))))


@pytest.fixture
def fake_build(scad, monkeypatch):
    """Records which meshes a build got and returns the first one (or a cube)."""
    import trimesh
    seen = []

    def _build(code, overrides, parts, deadline, meshes=None):
        seen.append({n: p.read_bytes() for n, p in (meshes or {}).items()})
        first = next(iter((meshes or {}).values()), None)
        return {"model": trimesh.load(first, file_type="stl") if first else trimesh.creation.box((1, 1, 1))}

    monkeypatch.setattr(openscad, "available", lambda: True)
    monkeypatch.setattr(openscad, "_build", _build)
    return seen


def test_openscad_on_an_imported_mesh_for_a_member(fake_build):
    import base64
    import trimesh
    joe = server.oauth.state.create_user("Joe")       # no admin needed
    stl = base64.b64encode(trimesh.creation.box((10, 20, 30)).export(file_type="stl")).decode()
    server.import_file("scan", data_base64=stl, ctx=ctx(joe))
    s = run(server.create_from_openscad("Drilled", 'difference() { import("base.stl"); cylinder(d=3, h=99); }',
                                        meshes={"base": "scan.stl"}, ctx=ctx(joe)))
    assert s["parts"]["model"]["extents_mm"] == [10.0, 20.0, 30.0]
    sha = server.get_model(s["model_id"], ctx=ctx(joe))["params"]["meshes"]["base"]
    assert openscad.SHA.fullmatch(sha) and (server.store.mesh_dir / f"{sha}.stl").is_file()
    # the recipe keeps its own copy: a changed or deleted file does not break a rebuild
    (server.Lib(joe).dir / "scan.stl").unlink()
    s = run(server.update_model(s["model_id"], {"d": 4}, ctx=ctx(joe)))
    assert s["version"] == 2 and fake_build[-1]["base"] == fake_build[0]["base"]
    # replace the mesh with another file, keeping the code
    server.import_file("bigger", data_base64=base64.b64encode(
        trimesh.creation.box((40, 20, 30)).export(file_type="stl")).decode(), ctx=ctx(joe))
    s = run(server.update_model(s["model_id"], meshes={"base": "bigger.stl"}, ctx=ctx(joe)))
    assert s["parts"]["model"]["extents_mm"] == [40.0, 20.0, 30.0]


def test_meshes_only_from_files_the_user_can_read(fake_build):
    import base64
    import trimesh
    joe, ann = server.oauth.state.create_user("Joe"), server.oauth.state.create_user("Ann")
    stl = base64.b64encode(trimesh.creation.box().export(file_type="stl")).decode()
    server.import_file("secret", data_base64=stl, ctx=ctx(ann))
    with pytest.raises(FileNotFoundError):
        run(server.create_from_openscad("x", 'import("base.stl");', meshes={"base": "secret.stl"}, ctx=ctx(joe)))
    m = run(server.create_model("annbox", {"root": {"shape": "box", "size": [1, 1, 1]}}, ctx=ctx(ann)))
    with pytest.raises(Exception):
        run(server.create_from_openscad("x", 'import("base.stl");',
                                        meshes={"base": f"{m['model_id']}/v1/model.stl"}, ctx=ctx(joe)))
