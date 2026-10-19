"""subscribe() in web/common.js: the page must catch up after a dropped or suspended stream.

Runs the real common.js under node with a fake EventSource (skipped when node is missing).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

COMMON_JS = Path(__file__).resolve().parent.parent / "web" / "common.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8").replace(/^export /gm, "");
const sources = [];
class ES {
  constructor(url) { this.url = url; this.closed = false; sources.push(this); }
  close() { this.closed = true; }
  send(data) { this.onmessage({ data: JSON.stringify(data) }); }
}
const timers = [];
const docListeners = {};
const winListeners = {};
global.EventSource = ES;
global.setTimeout = (f) => { timers.push(f); return timers.length; };
global.clearTimeout = () => {};
global.document = { hidden: false, addEventListener: (n, f) => { docListeners[n] = f; } };
global.window = { addEventListener: (n, f) => { winListeners[n] = f; } };
const { subscribe } = new Function(src + "; return { subscribe };")();
const calls = [];
const cls = new Set();
const label = { textContent: "Live" };
const statusEl = { title: "", querySelector: () => label,
  classList: { toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)) } };
const status = () => ({ classes: [...cls].sort(), label: label.textContent });
subscribe((d) => calls.push(d), statusEl);
const cur = () => sources[sources.length - 1];
const drop = () => { cur().onerror(); timers.splice(0).forEach((f) => f()); };
const scenario = {
  change() { cur().send({ revision: 1 }); cur().send({ revision: 2, id: "m" }); },
  missed_while_dropped() { cur().send({ revision: 1 }); drop(); cur().send({ revision: 5 }); },
  nothing_missed() { cur().send({ revision: 1 }); drop(); cur().send({ revision: 1 }); },
  foreground() {
    cur().send({ revision: 1 });
    const first = cur();
    document.hidden = false;
    docListeners.visibilitychange();
    cur().send({ revision: 3 });
    calls.push({ reconnected: cur() !== first, oldClosed: first.closed });
  },
  claude_status() {
    cur().send({ revision: 1, claude: false });
    calls.push(status());
    cur().send({ revision: 1, claude: true });
    calls.push(status());
    cur().send({ revision: 1, ok: false });
    calls.push(status());
    cur().send({ revision: 1, ok: true });
    drop();
    calls.push(status());
  },
  anonymous_status() { cur().send({ revision: 1, claude: null }); calls.push(status()); },
  bfcache() {
    cur().send({ revision: 1 });
    winListeners.pageshow({ persisted: true });
    cur().send({ revision: 2 });
  },
}[process.argv[3]];
scenario();
console.log(JSON.stringify(calls));
"""


def run(scenario):
    out = subprocess.run(["node", "-e", HARNESS, "-", str(COMMON_JS), scenario],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_change_event_is_passed_on():
    assert run("change") == [{"revision": 2, "id": "m"}]


def test_changes_made_while_disconnected_trigger_a_full_resync():
    calls = run("missed_while_dropped")
    assert calls and calls[-1].get("resync") is True


def test_reconnect_without_changes_stays_quiet():
    assert run("nothing_missed") == []


def test_returning_to_the_foreground_reconnects_and_catches_up():
    calls = run("foreground")
    assert {"reconnected": True, "oldClosed": True} in calls
    assert any(c.get("resync") is True for c in calls)


def test_restored_from_back_forward_cache_catches_up():
    calls = run("bfcache")
    assert any(c.get("resync") is True for c in calls)


def test_live_dot_waits_for_claude_then_turns_green():
    assert run("claude_status") == [
        {"classes": ["wait"], "label": "Waiting for AI"},
        {"classes": ["on"], "label": "Live"},
        {"classes": ["down"], "label": "Server problem"},
        {"classes": ["down"], "label": "Offline"},
    ]


def test_live_dot_is_green_for_anonymous_viewers():
    assert run("anonymous_status") == [{"classes": ["on"], "label": "Live"}]
