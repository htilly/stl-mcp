import json

import pytest
import trimesh

import store
from store import Store, apply_changes, ptr_remove, ptr_set, slug

CSG = {"vars": {"w": 20}, "root": {"op": "difference", "children": [
    {"shape": "box", "size": ["=w", "=w", 10]},
    {"shape": "cylinder", "d": 4, "h": 30, "translate": [10, 10, -5], "name": "hole"},
]}}
CSG_PARTS = {"parts": {"a": {"shape": "box", "size": [10, 10, 10]},
                       "b": {"shape": "cylinder", "d": 6, "h": 8}}}
ENCLOSURE = {"inner_x": 40, "inner_y": 30, "inner_z": 15}
PRIMITIVE = {"shape": "box", "x": 10, "y": 20, "z": 30}


@pytest.fixture
def st(tmp_path):
    return Store(tmp_path)


# ---- JSON pointers

def test_ptr_set_dict_and_list():
    doc = {"a": {"b": [1, 2, 3]}}
    ptr_set(doc, "/a/b/1", 20)
    ptr_set(doc, "/a/c", "new")
    ptr_set(doc, "/a/b/-", 4)
    assert doc == {"a": {"b": [1, 20, 3, 4], "c": "new"}}


def test_ptr_escapes():
    doc = {"a/b": {"c~d": 1}}
    ptr_set(doc, "/a~1b/c~0d", 2)
    assert doc["a/b"]["c~d"] == 2


def test_ptr_must_start_with_slash():
    with pytest.raises(ValueError, match="must start with"):
        ptr_set({}, "a", 1)


def test_ptr_missing_key_and_bad_descent():
    with pytest.raises(KeyError, match="missing"):
        ptr_set({"a": {}}, "/x/y", 1)
    with pytest.raises(KeyError, match="cannot descend"):
        ptr_set({"a": 5}, "/a/b/c", 1)


def test_ptr_remove_dict_and_list():
    doc = {"a": [1, 2, 3], "b": 1}
    ptr_remove(doc, "/a/0")
    ptr_remove(doc, "/b")
    assert doc == {"a": [2, 3]}
    with pytest.raises(KeyError):
        ptr_remove(doc, "/nope")


def test_apply_changes_csg_plain_key_goes_to_vars():
    p = apply_changes("csg", CSG, {"w": 30}, None)
    assert p["vars"]["w"] == 30 and CSG["vars"]["w"] == 20  # input untouched


def test_apply_changes_non_csg_plain_key_goes_to_params():
    p = apply_changes("enclosure", ENCLOSURE, {"wall": 3}, None)
    assert p["wall"] == 3


def test_apply_changes_pointer_and_remove():
    p = apply_changes("csg", CSG, {"/root/children/1/d": 8}, ["/root/children/1/name"])
    assert p["root"]["children"][1]["d"] == 8
    assert "name" not in p["root"]["children"][1]


# ---- slug

@pytest.mark.parametrize("name,expected", [
    ("My Box!", "my-box"),
    ("Åke Öberg", "ake-oberg"),
    ("  --  ", "model"),
    ("", "model"),
    ("a" * 80, "a" * 50),
])
def test_slug(name, expected):
    assert slug(name) == expected


# ---- create / read

@pytest.mark.parametrize("kind,params,parts", [
    ("csg", CSG, {"model"}),
    ("csg", CSG_PARTS, {"a", "b"}),
    ("enclosure", ENCLOSURE, {"body", "lid"}),
    ("primitive", PRIMITIVE, {"model"}),
])
def test_create_builds_all_kinds(st, tmp_path, kind, params, parts):
    m = st.create("Thing", kind, params, description="d")
    v = m["versions"][0]
    assert m["id"] == "thing" and m["current"] == 1 and m["description"] == "d"
    assert set(v["parts"]) == parts and v["note"] == "created"
    for info in v["parts"].values():
        assert (tmp_path / "models" / "thing" / info["file"]).is_file()
        assert info["watertight"] and info["triangles"] > 0
    assert (tmp_path / "models" / "thing" / "model.json").is_file()


def test_create_primitive_sits_on_plate(st):
    m = st.create("p", "primitive", {"shape": "sphere", "diameter": 10})
    assert m["versions"][0]["parts"]["model"]["bounds_mm"][0][2] == 0


def test_create_unique_ids(st):
    ids = [st.create("Same", "primitive", PRIMITIVE)["id"] for _ in range(3)]
    assert ids == ["same", "same-2", "same-3"]


def test_create_unknown_kind_leaves_no_trace(st):
    with pytest.raises(ValueError, match="unknown kind"):
        st.create("bad", "nonsense", {})
    assert st.list() == [] and not (st.dir / "bad").exists()


def test_create_failed_build_leaves_no_trace(st):
    with pytest.raises(ValueError):
        st.create("bad", "csg", {"root": {"shape": "nonsense"}})
    assert not (st.dir / "bad").exists()


def test_create_empty_part_error(st):
    with pytest.raises(ValueError):
        st.create("e", "csg", {"parts": {}})


def test_get_list_and_persistence(st, tmp_path):
    a = st.create("A", "primitive", PRIMITIVE)
    b = st.create("B", "primitive", PRIMITIVE)
    assert st.get("a")["name"] == "A"
    assert [m["id"] for m in st.list()] == [b["id"], a["id"]]  # newest first
    reopened = Store(tmp_path)
    assert reopened.get("a") == st.get("a")


def test_list_skips_corrupt_json(st):
    st.create("ok", "primitive", PRIMITIVE)
    bad = st.dir / "broken"
    bad.mkdir()
    (bad / "model.json").write_text("{not json")
    assert [m["id"] for m in st.list()] == ["ok"]


@pytest.mark.parametrize("mid", ["../etc", "A B", "", "x" * 61])
def test_invalid_ids(st, mid):
    with pytest.raises(KeyError, match="invalid model id"):
        st.get(mid)


def test_get_missing(st):
    with pytest.raises(KeyError, match="does not exist"):
        st.get("nope")


def test_revision_counter(st):
    r0 = st.revision
    m = st.create("r", "primitive", PRIMITIVE)
    assert st.revision > r0 and st.last_changed == m["id"]
    r1 = st.revision
    st.delete("r")
    assert st.revision > r1


def test_script_kind_disabled_by_default(st):
    with pytest.raises(PermissionError, match="disabled"):
        st.create("s", "script", {"code": "result = g.blk(1,1,1,0,0,0)"})


def test_script_kind_enabled(tmp_path):
    st = Store(tmp_path, allow_script=True)
    m = st.create("s", "script", {"code": "result = g.blk(2,3,4,0,0,0)"})
    assert m["versions"][0]["parts"]["model"]["extents_mm"] == [2.0, 3.0, 4.0]


def test_script_kind_named_parts_and_errors(tmp_path):
    st = Store(tmp_path, allow_script=True)
    m = st.create("s", "script", {"code": "parts = {'x': g.blk(1,1,1,0,0,0), 'y': g.blk(2,2,2,0,0,0)}"})
    assert set(m["versions"][0]["parts"]) == {"x", "y"}
    with pytest.raises(RuntimeError, match="boom"):
        st.create("t", "script", {"code": "raise Exception('boom')"})


# ---- update / versions

def test_update_changes_var_creates_version(st):
    st.create("m", "csg", CSG)
    m = st.update("m", changes={"w": 40})
    assert m["current"] == 2 and len(m["versions"]) == 2
    assert m["versions"][1]["params"]["vars"]["w"] == 40
    assert m["versions"][0]["params"]["vars"]["w"] == 20
    assert m["versions"][1]["parts"]["model"]["extents_mm"][:2] == [40.0, 40.0]
    assert m["versions"][1]["note"] == "w=40"


def test_update_with_pointer_remove_and_note(st):
    st.create("m", "csg", CSG)
    m = st.update("m", changes={"/root/children/1/d": 6}, remove=["/root/children/1/name"], note="bigger hole")
    assert m["versions"][-1]["note"] == "bigger hole"
    assert "name" not in m["versions"][-1]["params"]["root"]["children"][1]


def test_update_auto_note_for_remove_and_new_recipe(st):
    st.create("m", "csg", CSG)
    m = st.update("m", remove=["/root/children/1/name"])
    assert m["versions"][-1]["note"] == "-/root/children/1/name"
    m = st.update("m", params=CSG_PARTS)
    assert m["versions"][-1]["note"] == "new recipe"
    assert set(m["versions"][-1]["parts"]) == {"a", "b"}


def test_update_failure_saves_nothing(st):
    st.create("m", "csg", CSG)
    before = st.get("m")
    with pytest.raises((KeyError, IndexError)):
        st.update("m", changes={"/root/children/9/d": 1})
    assert st.get("m") == before
    assert not (st.dir / "m" / "v2").exists()


def test_update_name_and_description_only_makes_no_version(st):
    st.create("m", "primitive", PRIMITIVE)
    m = st.update("m", name="Renamed", description="new desc")
    assert m["name"] == "Renamed" and m["description"] == "new desc"
    assert len(m["versions"]) == 1 and m["id"] == "m"


def test_update_non_csg_param(st):
    st.create("e", "enclosure", ENCLOSURE)
    m = st.update("e", changes={"inner_x": 60})
    assert m["versions"][-1]["parts"]["body"]["extents_mm"][0] == pytest.approx(64.8)


def test_revert_creates_new_version_with_old_params(st):
    st.create("m", "csg", CSG)
    st.update("m", changes={"w": 40})
    m = st.revert("m", 1)
    assert m["current"] == 3 and m["versions"][2]["params"]["vars"]["w"] == 20
    assert m["versions"][2]["note"] == "reverted from v1"


def test_revert_unknown_version(st):
    st.create("m", "primitive", PRIMITIVE)
    with pytest.raises(KeyError, match="version 9"):
        st.revert("m", 9)


def test_set_current(st):
    st.create("m", "csg", CSG)
    st.update("m", changes={"w": 40})
    m = st.set_current("m", 1)
    assert m["current"] == 1 and len(m["versions"]) == 2
    assert Store.current(m)["version"] == 1


def test_pruning_old_versions(st, monkeypatch):
    monkeypatch.setattr(store, "MAX_VERSIONS", 2)
    st.create("m", "csg", CSG)
    for w in (21, 22, 23):
        m = st.update("m", changes={"w": w})
    assert [v.get("pruned", False) for v in m["versions"]] == [True, True, False, False]
    assert not (st.dir / "m" / "v1").exists() and (st.dir / "m" / "v4").is_dir()
    with pytest.raises(ValueError, match="pruned"):
        st.set_current("m", 1)
    m = st.revert("m", 1)  # recipe is kept, so revert rebuilds it
    assert m["versions"][-1]["params"]["vars"]["w"] == 20
    assert (st.dir / "m" / f"v{m['current']}").is_dir()


def test_duplicate(st):
    st.create("orig", "csg", CSG, description="desc")
    st.update("orig", changes={"w": 40})
    d = st.duplicate("orig", "Copy")
    assert d["id"] == "copy" and d["description"] == "desc"
    assert d["versions"][0]["params"]["vars"]["w"] == 40
    assert d["versions"][0]["note"] == "copy of orig v2"
    assert st.get("orig")["current"] == 2


def test_delete(st):
    st.create("m", "primitive", PRIMITIVE)
    st.delete("m")
    assert st.list() == [] and not (st.dir / "m").exists()
    with pytest.raises(KeyError):
        st.delete("m")


def test_file_lookup_and_traversal_protection(st):
    m = st.create("m", "primitive", PRIMITIVE)
    rel = m["versions"][0]["parts"]["model"]["file"]
    assert st.file("m", rel).is_file()
    for bad in ("../m/model.json", "model.json", "v1/missing.stl", "../../etc/passwd"):
        with pytest.raises(KeyError):
            st.file("m", bad)


# ---- comments

def test_add_comment_basic(st):
    st.create("m", "primitive", PRIMITIVE)
    c = st.add_comment("m", "  too thin  ", point=[1.234, 2, 3], normal=[0, 0, 1.00049], part="model")
    assert c["id"] == "c1" and c["text"] == "too thin" and c["status"] == "open"
    assert c["author"] == "user" and c["version"] == 1 and c["replies"] == []
    assert c["point"] == [1.23, 2.0, 3.0] and c["normal"] == [0.0, 0.0, 1.0]
    assert st.get("m")["comments"] == [c]
    assert st.add_comment("m", "second")["id"] == "c2"


def test_add_comment_validation(st):
    st.create("m", "primitive", PRIMITIVE)
    with pytest.raises(ValueError, match="empty"):
        st.add_comment("m", "   ")
    with pytest.raises(KeyError, match="version 5"):
        st.add_comment("m", "x", version=5)
    with pytest.raises(KeyError):
        st.add_comment("nope", "x")


def test_add_comment_truncates_text(st):
    st.create("m", "primitive", PRIMITIVE)
    assert len(st.add_comment("m", "x" * 5000)["text"]) == 2000


def test_comment_hints_csg(st):
    st.create("m", "csg", CSG)
    c = st.add_comment("m", "hole here", point=[10, 10, 5], part="model")
    nodes = c["near_nodes"]
    assert nodes[0]["pointer"] == "/root/children/1" and nodes[0]["name"] == "hole"
    assert nodes[0]["distance_mm"] == 0


def test_comment_hints_enclosure(st):
    st.create("e", "enclosure", ENCLOSURE)
    body = st.add_comment("e", "x", point=[5.4, 6.4, 7.4], part="body")
    assert body["inner_xyz"] == [3.0, 4.0, 5.0]
    lid = st.add_comment("e", "x", point=[5.4, 6.4, 1], part="lid")
    assert lid["inner_xy"] == [3.0, 4.0] and "inner_xyz" not in lid


def test_comment_without_point_has_no_hints(st):
    st.create("m", "csg", CSG)
    c = st.add_comment("m", "general")
    assert "point" not in c and "near_nodes" not in c


def test_comment_hints_are_best_effort(st, monkeypatch):
    st.create("m", "csg", CSG)

    def boom(*a, **kw):
        raise RuntimeError("locate failed")

    monkeypatch.setattr(store.csg, "locate", boom)
    c = st.add_comment("m", "x", point=[1, 2, 3], part="model")
    assert "near_nodes" not in c and c["point"] == [1.0, 2.0, 3.0]


def test_comment_actions(st):
    st.create("m", "primitive", PRIMITIVE)
    st.add_comment("m", "c")
    m = st.comment_action("m", "c1", "reply", "ok", author="claude")
    r = m["comments"][0]["replies"][0]
    assert r["author"] == "claude" and r["text"] == "ok" and r["version"] == 1
    assert m["comments"][0]["status"] == "open"
    m = st.comment_action("m", "c1", "resolve", "done")
    assert m["comments"][0]["status"] == "resolved" and len(m["comments"][0]["replies"]) == 2
    m = st.comment_action("m", "c1", "reopen")
    assert m["comments"][0]["status"] == "open"
    m = st.comment_action("m", "c1", "reply", "   ")  # blank replies are ignored
    assert len(m["comments"][0]["replies"]) == 2
    m = st.comment_action("m", "c1", "delete")
    assert m["comments"] == []


def test_comment_action_errors(st):
    st.create("m", "primitive", PRIMITIVE)
    st.add_comment("m", "c")
    with pytest.raises(KeyError, match="does not exist"):
        st.comment_action("m", "c9", "resolve")
    with pytest.raises(KeyError):
        st.comment_action("m", "c9", "delete")
    with pytest.raises(ValueError, match="unknown comment action"):
        st.comment_action("m", "c1", "explode")


def test_update_resolves_comments(st):
    st.create("m", "csg", CSG)
    st.add_comment("m", "make it wider")
    m = st.update("m", changes={"w": 30}, resolves=["c1"], note="widened")
    c = m["comments"][0]
    assert c["status"] == "resolved"
    assert c["replies"][-1]["author"] == "claude"
    assert c["replies"][-1]["text"] == "Addressed in v2: widened"


def test_update_resolves_unknown_comment_before_building(st):
    st.create("m", "csg", CSG)
    with pytest.raises(KeyError, match="comment 'c7'"):
        st.update("m", changes={"w": 30}, resolves=["c7"])
    assert len(st.get("m")["versions"]) == 1


def test_comments_survive_new_versions(st):
    st.create("m", "csg", CSG)
    st.add_comment("m", "keep me")
    m = st.update("m", changes={"w": 30})
    assert [c["text"] for c in m["comments"]] == ["keep me"]
    assert json.loads((st.dir / "m" / "model.json").read_text())["comments"][0]["text"] == "keep me"


def test_stats_helper():
    s = store._stats(trimesh.creation.box(extents=(10, 10, 10)))
    assert s["watertight"] and s["volume_cm3"] == 1.0 and s["triangles"] == 12
