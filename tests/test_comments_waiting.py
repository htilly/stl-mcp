"""Comments are not pushed to Claude, so tool results tell it which models have open comments."""
from types import SimpleNamespace

import server


def _two_users(out_dir):
    st = server.oauth.state
    return st.create_user("Joe", "joe@example.com"), st.create_user("Ann", "ann@example.com")


def _model(user, name):
    lib = server.Lib(user)
    return lib, lib.models.create(name, "csg", server.BLANK_SPEC, "", "test")


def test_nothing_waiting_adds_no_field(out_dir):
    joe, _ = _two_users(out_dir)
    lib, m = _model(joe, "box")
    assert "comments_waiting" not in server._summary(m, lib=lib)


def test_open_comment_shows_in_summary_and_whoami(out_dir):
    joe, _ = _two_users(out_dir)
    lib, m = _model(joe, "box")
    _, other = _model(joe, "lid")
    lib.models.add_comment(m["id"], "make the hole 2 mm larger")
    waiting = server._summary(other, lib=lib)["comments_waiting"]
    assert [(w["model_id"], w["open"]) for w in waiting] == [(m["id"], 1)]
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        state=SimpleNamespace(user=joe))))
    assert server.whoami(ctx)["comments_waiting"][0]["model_id"] == m["id"]
    assert server._waiting(lib)[0]["viewer_url"].endswith(f"/m/{m['id']}")


def test_resolved_comments_are_not_waiting(out_dir):
    joe, _ = _two_users(out_dir)
    lib, m = _model(joe, "box")
    c = lib.models.add_comment(m["id"], "bigger")
    lib.models.comment_action(m["id"], c["id"], "resolve")
    assert server._waiting(lib) == []


def test_other_users_comments_are_not_visible(out_dir):
    joe, ann = _two_users(out_dir)
    jlib, jm = _model(joe, "joe-box")
    alib, am = _model(ann, "ann-box")
    jlib.models.add_comment(jm["id"], "for joe")
    assert server._waiting(alib) == []
    assert "comments_waiting" not in server._summary(am, lib=alib)


def test_instructions_tell_claude_to_act_on_waiting_comments():
    assert "comments_waiting" in server.mcp.instructions
