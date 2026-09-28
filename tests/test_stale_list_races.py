"""A response built before a local change must not undo that change.

Two races with one shape: the server builds a list, a local action lands, then
the old list arrives and is obeyed.

1. Tab lists (js/target-hold.js). /poll lists panes first and then scans each
   one, so a poll in flight when Duplicate or Launch returns delivered a list
   without the new session. The tab strip read that as "the pane closed" and
   moved to the first tab, saving it — you landed on a random pane.
2. Drafts (js/drafts.js, shared/drafts.py:poll_block). A poll whose draft
   snapshot predated the first save of a message, delivered after that save's
   ack, lacked the row, read as "deleted on another device", and blanked a
   composer the user was still writing in.

Both directions are pinned: the stale response is ignored, a fresh one still
wins, so the guard cannot quietly swallow a real close or a real remote send.
"""

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def run_js(files, body, prelude=""):
    harness = r"""
const fs = require('fs');
const vm = require('vm');
globalThis.clock = 1000;
Date.now = () => clock;
PRELUDE
for (const f of FILES) vm.runInThisContext(fs.readFileSync(f, 'utf8'), {filename: f});
(async () => {
BODY
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
""".replace("FILES", json.dumps(files)).replace("PRELUDE", prelude).replace("BODY", body)
    result = subprocess.run(
        ["node", "-e", harness], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class TargetHoldTests(unittest.TestCase):
    def test_list_requested_before_a_new_pane_cannot_drop_it(self):
        result = run_js(["js/target-hold.js"], r"""
const staleRequest = clock;          // poll sent...
clock += 300;
noteTargetChosen('dup:0.0', true);   // ...Duplicate returns...
clock += 700;                        // ...the old list lands
const stale = targetMissingIsStale('dup:0.0', staleRequest);
const freshButEarly = targetMissingIsStale('dup:0.0', clock);   // held until listed
noteTargetListed('dup:0.0');
clock += 10;
const afterListed = targetMissingIsStale('dup:0.0', clock);
console.log(JSON.stringify({stale, freshButEarly, afterListed}));
""")
        self.assertTrue(result["stale"])
        self.assertTrue(result["freshButEarly"])
        self.assertFalse(result["afterListed"], "a pane that was listed and then closed must fall back")

    def test_hold_expires_and_does_not_cover_other_panes(self):
        result = run_js(["js/target-hold.js"], r"""
noteTargetChosen('dup:0.0', true);
clock += 10;
const otherPane = targetMissingIsStale('old:0.0', clock);
clock += 16000;
const expired = targetMissingIsStale('dup:0.0', clock);
console.log(JSON.stringify({otherPane, expired}));
""")
        self.assertFalse(result["otherPane"])
        self.assertFalse(result["expired"], "a pane that never appears must not pin the view forever")

    def test_a_tap_protects_only_against_older_lists(self):
        result = run_js(["js/target-hold.js"], r"""
noteTargetChosen('a:0.0', false);
const before = targetMissingIsStale('a:0.0', clock - 5);
clock += 5;
const after = targetMissingIsStale('a:0.0', clock);
console.log(JSON.stringify({before, after}));
""")
        self.assertTrue(result["before"])
        self.assertFalse(result["after"], "a tapped pane that later closes must still fall back")

    def test_every_fallback_and_creation_path_is_wired(self):
        app = (ROOT / "js/app.js").read_text()
        terminal = (ROOT / "js/terminal.js").read_text()
        tabs = (ROOT / "js/tabs.js").read_text()
        actions = (ROOT / "js/actions.js").read_text()
        for name, src in (("app.js", app), ("terminal.js", terminal)):
            self.assertIn("targetMissingIsStale(current, requestedAt", src, name)
            self.assertIn("noteTargetListed(current)", src, name)
        self.assertEqual(terminal.count("noteTargetChosen(_termTarget, true)"), 2)
        self.assertIn("noteTargetChosen(_termTarget, true)", actions)
        self.assertIn("noteTargetChosen(target, false)", terminal)
        dup = tabs[tabs.index("'/terminal/duplicate'"):tabs.index("'Duplicate failed'")]
        self.assertIn("noteTargetChosen(data.target, true)", dup)
        self.assertIn("selectTab(data.target, true)", dup)
        self.assertNotIn("_termTarget = data.target", dup)

    def test_module_loads_before_its_callers(self):
        html = (ROOT / "index.html").read_text()
        pos = html.index("/js/target-hold.js")
        for caller in ("/js/terminal.js", "/js/actions.js", "/js/tabs.js", "/js/app.js"):
            self.assertLess(pos, html.index(caller), caller)


DRAFTS_PRELUDE = r"""
const noop = () => {};
globalThis.listeners = {};
globalThis.input = {
    value: '',
    addEventListener(type, fn) { listeners[type] = fn; },
    setAttribute: noop,
    blur: noop, focus: noop,
};
globalThis.document = {
    querySelectorAll: () => [],
    getElementById: () => null,
    addEventListener: noop,
    activeElement: null,
};
globalThis._termTarget = 's:0.0';
globalThis._attachments = [];
globalThis.renderAttachments = noop;
globalThis.flashes = [];
globalThis.showFlash = (t, m) => flashes.push(m);
globalThis.timers = [];
globalThis.setTimeout = fn => { timers.push(fn); return timers.length; };
globalThis.clearTimeout = noop;
globalThis.runTimers = () => { const t = timers.splice(0); t.forEach(fn => fn()); };
globalThis.serverDraft = {text: '', attachments: [], enter_armed: true, updated_at: 0};
globalThis.fetch = async (url, opts) => {
    if (opts && opts.method === 'PUT') {
        const body = JSON.parse(opts.body);
        serverDraft = {text: body.text, attachments: [], enter_armed: true, updated_at: 200};
    }
    return {json: async () => ({ok: true, draft: serverDraft})};
};
globalThis.tick = () => new Promise(r => setImmediate(r));
"""

TYPE_AND_SAVE = r"""
_applyDraftsData({marks: [], rev: {}, at: 100});   // page load: first sight
await tick(); await tick();
input.value = 'half a thought';
listeners.input();
runTimers();                                        // debounce fires, PUT acked at 200
await tick(); await tick();
"""


class DraftStalePollTests(unittest.TestCase):
    def test_poll_snapshot_older_than_our_save_keeps_the_composer(self):
        result = run_js(["js/drafts.js"], TYPE_AND_SAVE + r"""
_applyDraftsData({marks: [], rev: {}, at: 150});    // built before the PUT landed
console.log(JSON.stringify({text: input.value}));
""", DRAFTS_PRELUDE)
        self.assertEqual(result["text"], "half a thought")

    def test_newer_snapshot_without_the_row_still_clears(self):
        result = run_js(["js/drafts.js"], TYPE_AND_SAVE + r"""
_applyDraftsData({marks: [], rev: {}, at: 250});    // sent from another device
console.log(JSON.stringify({text: input.value}));
""", DRAFTS_PRELUDE)
        self.assertEqual(result["text"], "", "a real remote send or expiry must still clear")

    def test_poll_block_carries_its_snapshot_time(self):
        from shared import drafts

        with patch.object(drafts.time, "time", return_value=1234.5):
            block = drafts.poll_block()
        self.assertEqual(block["at"], 1234.5)
        self.assertIn("rev", block)
        self.assertIn("marks", block)


if __name__ == "__main__":
    unittest.main()
