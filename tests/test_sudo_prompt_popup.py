"""A sudo prompt in any pane surfaces as a 5-second popup with Yes / No.

Three seams, each with fixtures in both directions:

1. Detection (shared/tmux.py:sudo_prompt_waiting). A pane qualifies only when
   sudo's own prompt is its last line AND a childless sudo process is in the
   foreground of its tty. The prompt line alone can be printed by anything; an
   authenticated sudo running a silent command leaves the same line on screen but
   has forked a child.
2. The answer guard (`expect_prompt_pid` on /type and /key). A tap lands seconds
   after the poll that raised the popup. If that sudo has stopped waiting, a
   password typed with Enter would run in a shell and land in its history.
3. The popup (js/sudo-prompt.js), run under Node with a stub DOM: once per sudo
   process, gone after 5 s, never takes focus, and Yes never claims a send the
   vault could not make.

Run: .venv/bin/python3 -m unittest tests.test_sudo_prompt_popup
"""

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routes.input import input_bp
from shared import tmux
from shared.tmux import DeliveryResult, ExpectedTargetIdentity


ROOT = Path(__file__).resolve().parents[1]

PROMPT_TAIL = "daniel@host:~$ sudo apt install tmux\n[sudo] password for daniel: "
# bash (101) is the parent of sudo (202); nothing is sudo's child yet.
WAITING_TTY = "  101 Ss   -bash\n  202 S+   sudo apt install tmux\n"
PARENTS_WAITING = "    1\n  100\n  101\n"
PARENTS_AUTHENTICATED = PARENTS_WAITING + "  202\n"


def _completed(stdout, returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _host(on_tty=WAITING_TTY, parents=PARENTS_WAITING, tty="/dev/pts/9", screen=PROMPT_TAIL):
    """subprocess.run stand-in for the tmux and ps calls the detector makes."""
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["ps", "-t"]:
            return _completed(on_tty)
        if argv[:2] == ["ps", "-A"]:
            return _completed(parents)
        if argv[:2] == ["tmux", "display-message"]:
            return _completed(tty + "\n")
        if argv[:2] == ["tmux", "capture-pane"]:
            return _completed(screen + "\n\n")
        raise AssertionError(f"unexpected command {argv}")

    run.calls = calls
    return run


def _no_subprocess(argv, **_kwargs):
    raise AssertionError(f"detector shelled out for a pane with no sudo prompt: {argv}")


class SudoPromptDetectionTests(unittest.TestCase):
    def detect(self, tail, **host):
        with patch("shared.tmux.subprocess.run", _host(**host)):
            return tmux.sudo_prompt_waiting("/dev/pts/9", tail)

    def test_a_waiting_sudo_is_reported_with_its_own_argv(self):
        self.assertEqual(
            self.detect(PROMPT_TAIL),
            {"pid": 202, "command": "sudo apt install tmux"},
        )

    def test_escape_sequences_around_the_prompt_do_not_hide_it(self):
        tail = "\x1b[32mdaniel@host\x1b[0m:~$ sudo true\n\x1b[0m[sudo] password for daniel: \x1b[K\n\n"
        self.assertEqual(self.detect(tail)["pid"], 202)

    def test_sudo_by_full_path_still_counts(self):
        on_tty = "  101 Ss   -bash\n  202 S+   /usr/bin/sudo -E make install\n"
        self.assertEqual(
            self.detect(PROMPT_TAIL, on_tty=on_tty)["command"],
            "/usr/bin/sudo -E make install",
        )

    def test_authenticated_sudo_with_its_prompt_still_on_screen_is_not_waiting(self):
        self.assertIsNone(self.detect(PROMPT_TAIL, parents=PARENTS_AUTHENTICATED))

    def test_a_printed_prompt_with_no_sudo_process_is_ignored(self):
        on_tty = "  101 Ss   -bash\n  303 S+   cat\n"
        self.assertIsNone(self.detect(PROMPT_TAIL, on_tty=on_tty))

    def test_a_background_sudo_is_ignored(self):
        on_tty = "  101 Ss+  -bash\n  202 S    sudo apt install tmux\n"
        self.assertIsNone(self.detect(PROMPT_TAIL, on_tty=on_tty, parents="1\n100\n"))

    def test_a_prompt_that_is_no_longer_the_last_line_never_runs_ps(self):
        tail = PROMPT_TAIL + "\nsudo: a password is required\ndaniel@host:~$ "
        with patch("shared.tmux.subprocess.run", _no_subprocess):
            self.assertIsNone(tmux.sudo_prompt_waiting("/dev/pts/9", tail))

    def test_other_password_prompts_do_not_qualify(self):
        for line in (
            "daniel@host's password: ",
            "Enter passphrase for key '/home/daniel/.ssh/id_ed25519': ",
            "Password: ",
            "echo sudo; Enter password: ",
        ):
            with self.subTest(line=line):
                self.assertIsNone(self.detect("$ sudo ssh host\n" + line))


class PromptOwnerGuardTests(unittest.TestCase):
    def guard(self, pid, **host):
        run = _host(**host)
        with patch("shared.tmux.subprocess.run", run):
            return tmux.prompt_owner_waiting("%9", pid), run.calls

    def test_the_waiting_sudo_passes_and_the_pane_id_is_used_as_is(self):
        ok, calls = self.guard(202)
        self.assertTrue(ok)
        tmux_targets = [argv[argv.index("-t") + 1] for argv in calls if argv[0] == "tmux"]
        self.assertEqual(tmux_targets, ["%9", "%9"])

    def test_a_different_process_fails(self):
        self.assertFalse(self.guard(999)[0])

    def test_a_sudo_that_authenticated_meanwhile_fails(self):
        self.assertFalse(self.guard(202, parents=PARENTS_AUTHENTICATED)[0])

    def test_a_pane_that_moved_on_to_a_shell_fails(self):
        screen = PROMPT_TAIL + "\nsudo: a password is required\ndaniel@host:~$ "
        self.assertFalse(self.guard(202, screen=screen)[0])

    def test_malformed_pids_fail_without_asking_tmux(self):
        for pid in ("abc", None, True, [202]):
            with self.subTest(pid=pid), patch("shared.tmux.subprocess.run", _no_subprocess):
                self.assertFalse(tmux.prompt_owner_waiting("%9", pid))


def _expected_identity():
    return ExpectedTargetIdentity(
        socket_path="/tmp/assist-test-tmux.sock",
        socket_device=1,
        socket_inode=2,
        server_pid=3,
        server_start_time="4",
        session_id="$1",
        window_id="@1",
        pane_id="%1",
        pane_pid=5,
        pane_start_time="6",
    )


class PromptAnswerRouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.logger.disabled = True
        app.register_blueprint(input_bp)
        self.client = app.test_client()
        self.expected = _expected_identity()
        patches = [
            patch("routes.input.expected_target_identity", return_value=self.expected),
            patch("routes.input.add_to_history"),
            patch("routes.input.declare_agent_command"),
            patch("routes.input.state.touch_activity"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def post(self, url, body, waiting):
        with patch(
            "routes.input.prompt_owner_waiting", return_value=waiting
        ) as guard, patch(
            "routes.input.generation_bound_delivery",
            return_value=DeliveryResult("delivered"),
        ) as deliver:
            response = self.client.post(url, json=body)
        return response, guard, deliver

    def secret_body(self, **extra):
        return dict(text=" pw with spaces ", enter=True, expand=False, secret=True,
                    target="del_build:0.0", **extra)

    def test_yes_to_a_prompt_that_stopped_waiting_types_nothing(self):
        response, guard, deliver = self.post(
            "/type", self.secret_body(expect_prompt_pid=202), waiting=False
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "prompt_gone")
        guard.assert_called_once_with("%1", 202)
        deliver.assert_not_called()

    def test_yes_to_a_waiting_prompt_types_the_secret_exactly(self):
        response, _guard, deliver = self.post(
            "/type", self.secret_body(expect_prompt_pid=202), waiting=True
        )
        self.assertEqual(response.status_code, 200)
        deliver.assert_called_once_with(self.expected, text=" pw with spaces ", enter=True)

    def test_type_without_a_named_prompt_is_unchanged(self):
        response, guard, deliver = self.post("/type", self.secret_body(), waiting=False)
        self.assertEqual(response.status_code, 200)
        guard.assert_not_called()
        deliver.assert_called_once()

    def test_no_to_a_prompt_that_stopped_waiting_interrupts_nothing(self):
        body = {"keys": "ctrl+c", "target": "del_build:0.0", "expect_prompt_pid": 202}
        response, guard, deliver = self.post("/key", body, waiting=False)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "prompt_gone")
        guard.assert_called_once_with("%1", 202)
        deliver.assert_not_called()

    def test_no_to_a_waiting_prompt_sends_ctrl_c(self):
        body = {"keys": "ctrl+c", "target": "del_build:0.0", "expect_prompt_pid": 202}
        response, _guard, deliver = self.post("/key", body, waiting=True)
        self.assertEqual(response.status_code, 200)
        deliver.assert_called_once_with(self.expected, keys=("C-c",))

    def test_key_without_a_named_prompt_is_unchanged(self):
        response, guard, deliver = self.post(
            "/key", {"keys": "ctrl+c", "target": "del_build:0.0"}, waiting=False
        )
        self.assertEqual(response.status_code, 200)
        guard.assert_not_called()
        deliver.assert_called_once()


def run_popup_js(body):
    """Execute sudo-prompt.js against a stub DOM and return BODY's JSON line."""
    harness = r"""
const fs = require('fs');
const vm = require('vm');

class FakeClassList {
    constructor() { this.names = new Set(); }
    add(name) { this.names.add(name); }
    remove(name) { this.names.delete(name); }
    contains(name) { return this.names.has(name); }
}

class FakeElement {
    constructor(tag) {
        this.tagName = tag;
        this.children = [];
        this.parentNode = null;
        this.classList = new FakeClassList();
        this.style = {};
        this.attributes = {};
        this.listeners = {};
        this.textContent = '';
        this.title = '';
        this.tabIndex = 0;
    }
    set className(value) {
        this.classList = new FakeClassList();
        String(value).split(/\s+/).filter(Boolean).forEach(n => this.classList.add(n));
    }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    remove() {
        if (!this.parentNode) return;
        const siblings = this.parentNode.children;
        siblings.splice(siblings.indexOf(this), 1);
        this.parentNode = null;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    focus() { focusCalls += 1; }
    fire(type, event) { for (const fn of this.listeners[type] || []) fn(event); }
    find(name) {
        if (this.classList.contains(name)) return this;
        for (const child of this.children) {
            const hit = child.find(name);
            if (hit) return hit;
        }
        return null;
    }
}

globalThis.focusCalls = 0;
globalThis.timers = [];
globalThis.setTimeout = (fn, ms) => {
    const t = {fn, ms, cleared: false, ran: false};
    timers.push(t);
    return t;
};
globalThis.clearTimeout = t => { if (t) t.cleared = true; };
globalThis.fireTimers = ms => {
    for (const t of timers.slice()) {
        if (t.ms === ms && !t.cleared && !t.ran) { t.ran = true; t.fn(); }
    }
};
globalThis.requestAnimationFrame = fn => fn();
globalThis.document = {body: new FakeElement('body'), createElement: tag => new FakeElement(tag)};
globalThis._sessionPanes = [];
globalThis.shortName = name => name.replace(/^[-\w]+?_/, '');
globalThis.agentDisplayName = pane => pane.agent_name ? pane.agent_name.replace(/-agent$/, '') : null;
globalThis.selectCalls = [];
globalThis.selectTab = target => { selectCalls.push(target); };
globalThis.flashes = [];
globalThis.showFlash = (type, text) => { flashes.push([type, text]); };
globalThis.lastAction = 0;
globalThis.updateStatusTime = () => {};
globalThis.vaultMap = {sudo: 'test-secret'};
globalThis.vaultHas = handle => Object.prototype.hasOwnProperty.call(vaultMap, handle);
globalThis.vaultGet = handle => vaultHas(handle) ? vaultMap[handle] : undefined;
globalThis.fetchReply = {ok: true};
globalThis.fetchCalls = [];
globalThis.fetch = async (url, options) => {
    fetchCalls.push({url, body: JSON.parse(options.body)});
    return {json: async () => fetchReply};
};
globalThis.sudoPrompt = {target: 'del_build:0.0', session: 'del_build', pid: 202,
                         command: 'sudo apt install tmux'};
globalThis.popups = () => document.body.children[0].children;
globalThis.tick = () => new Promise(resolve => setImmediate(resolve));
globalThis.click = (popup, name) => popup.find(name).fire('click', {stopPropagation() {}});

vm.runInThisContext(fs.readFileSync('js/sudo-prompt.js', 'utf8'), {filename: 'js/sudo-prompt.js'});

(async () => {
BODY
})().catch(error => {
    console.error(error.stack || error);
    process.exitCode = 1;
});
""".replace("BODY", body)
    result = subprocess.run(
        ["node", "-e", harness], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class SudoPopupClientTests(unittest.TestCase):
    def test_one_popup_per_sudo_process_gone_after_five_seconds(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
_applySudoPrompts([sudoPrompt]);
const shown = popups().length;
const delays = timers.map(t => t.ms);
fireTimers(5000);
fireTimers(200);
const afterTimeout = popups().length;
_applySudoPrompts([sudoPrompt]);
const stillWaiting = popups().length;
_applySudoPrompts([Object.assign({}, sudoPrompt, {pid: 303})]);
const nextSudo = popups().length;
console.log(JSON.stringify({shown, delays, afterTimeout, stillWaiting, nextSudo}));
""")
        self.assertEqual(result["shown"], 1)
        self.assertIn(5000, result["delays"])
        self.assertEqual(result["afterTimeout"], 0)
        self.assertEqual(result["stillWaiting"], 0)
        self.assertEqual(result["nextSudo"], 1)

    def test_popup_leaves_as_soon_as_sudo_stops_waiting(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
_applySudoPrompts([]);
fireTimers(200);
console.log(JSON.stringify({left: popups().length}));
""")
        self.assertEqual(result["left"], 0)

    def test_summary_names_the_pane_and_the_command(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
const popup = popups()[0];
console.log(JSON.stringify({
    where: popup.find('sudo-popup-where').textContent,
    cmd: popup.find('sudo-popup-cmd').textContent,
}));
""")
        self.assertEqual(result["where"], "sudo · build")
        self.assertEqual(result["cmd"], "apt install tmux")

    def test_yes_sends_the_vault_value_as_a_guarded_secret(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
const popup = popups()[0];
const label = popup.find('sudo-popup-yes').textContent;
click(popup, 'sudo-popup-yes');
await tick();
fireTimers(200);
console.log(JSON.stringify({label, fetchCalls, flashes, left: popups().length}));
""")
        self.assertEqual(result["label"], "Yes")
        self.assertEqual(result["fetchCalls"], [{
            "url": "/type",
            "body": {"text": "test-secret", "enter": True, "expand": False, "secret": True,
                     "target": "del_build:0.0", "expect_prompt_pid": 202},
        }])
        self.assertEqual(result["flashes"], [["sent", "Password sent"]])
        self.assertEqual(result["left"], 0)

    def test_no_sends_a_guarded_ctrl_c(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
click(popups()[0], 'sudo-popup-no');
await tick();
console.log(JSON.stringify({fetchCalls}));
""")
        self.assertEqual(result["fetchCalls"], [{
            "url": "/key",
            "body": {"keys": "ctrl+c", "target": "del_build:0.0", "expect_prompt_pid": 202},
        }])

    def test_without_a_stored_sudo_yes_opens_the_pane_and_sends_nothing(self):
        result = run_popup_js(r"""
vaultMap = {};
_applySudoPrompts([sudoPrompt]);
const popup = popups()[0];
const label = popup.find('sudo-popup-yes').textContent;
click(popup, 'sudo-popup-yes');
await tick();
console.log(JSON.stringify({label, selectCalls, fetchCalls, flashes}));
""")
        self.assertEqual(result["label"], "Open")
        self.assertEqual(result["selectCalls"], ["del_build:0.0"])
        self.assertEqual(result["fetchCalls"], [])
        self.assertEqual(result["flashes"], [])

    def test_a_refused_answer_is_reported_not_claimed(self):
        result = run_popup_js(r"""
fetchReply = {ok: false, error: 'prompt_gone'};
_applySudoPrompts([sudoPrompt]);
click(popups()[0], 'sudo-popup-yes');
await tick();
console.log(JSON.stringify({flashes}));
""")
        self.assertEqual(result["flashes"], [["error", "sudo is no longer waiting"]])

    def test_the_popup_never_takes_focus(self):
        result = run_popup_js(r"""
_applySudoPrompts([sudoPrompt]);
const popup = popups()[0];
let prevented = false;
popup.fire('pointerdown', {preventDefault() { prevented = true; }});
click(popup, 'sudo-popup-no');
await tick();
console.log(JSON.stringify({
    prevented,
    focusCalls,
    tabIndexes: [popup.find('sudo-popup-yes').tabIndex, popup.find('sudo-popup-no').tabIndex],
    selectCalls,
}));
""")
        self.assertTrue(result["prevented"])
        self.assertEqual(result["focusCalls"], 0)
        self.assertEqual(result["tabIndexes"], [-1, -1])
        self.assertEqual(result["selectCalls"], [])
        self.assertNotIn(".focus(", (ROOT / "js/sudo-prompt.js").read_text())

    def test_the_page_loads_the_popup_and_the_poll_feeds_it(self):
        html = (ROOT / "index.html").read_text()
        self.assertIn('src="/js/sudo-prompt.js', html)
        app = (ROOT / "js/app.js").read_text()
        self.assertIn("_applySudoPrompts(data.sudo_prompts", app)
        poll = (ROOT / "routes/poll.py").read_text()
        self.assertIn('result["sudo_prompts"] = sudo_prompts', poll)
        self.assertIn("#{pane_tty}", poll)


if __name__ == "__main__":
    unittest.main()
