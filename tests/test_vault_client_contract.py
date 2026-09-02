"""Guards for browser-only vault invariants.

Small behavior checks execute vault.js directly with Node; source guards pin
the browser seams that need DOM or traffic context. The companion Playwright
script asserts the same contract over recorded browser traffic.

Run: .venv/bin/python3 -m unittest tests.test_vault_client_contract
"""

import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path):
    return (ROOT / path).read_text()


def between(text, start, end):
    return text[text.index(start) : text.index(end, text.index(start))]


def run_vault_js(body):
    """Execute vault.js with the smallest browser-shaped surface it needs."""
    harness = r"""
const fs = require('fs');
const vm = require('vm');

globalThis._termTarget = 'vault-main:0.0';
globalThis._termLatestContent = '';
globalThis.inputTarget = _termTarget;
globalThis.lastAction = 0;
globalThis.getInputTarget = () => inputTarget;
globalThis.stripAnsi = value => value;
globalThis._lastNonEmptyLine = text => {
    const lines = String(text || '').split('\n');
    for (let i = lines.length - 1; i >= 0; i--) {
        if (lines[i].trim()) return lines[i];
    }
    return '';
};
globalThis.showFlash = () => {};
globalThis.updateStatusTime = () => {};

const button = {
    hidden: true,
    textContent: '',
    title: '',
    attributes: {},
    classList: {
        toggle(name, force) {
            if (name === 'hidden') button.hidden = !!force;
        },
    },
    setAttribute(name, value) { this.attributes[name] = value; },
};
globalThis.button = button;
globalThis.document = {
    getElementById(id) { return id === 'vault-quick-send' ? button : null; },
};
globalThis.fetchCalls = [];
globalThis.fetch = async (url, options) => {
    fetchCalls.push({url, body: JSON.parse(options.body)});
    return {json: async () => ({ok: true})};
};

vm.runInThisContext(fs.readFileSync('js/vault.js', 'utf8'), {filename: 'js/vault.js'});

globalThis.vaultMap = {sudo: 'test-secret'};
vaultSetAdapter({
    load() { return Object.assign({}, vaultMap); },
    save(next) { vaultMap = Object.assign({}, next); },
    available() { return true; },
    has(handle) { return Object.prototype.hasOwnProperty.call(vaultMap, handle); },
    handles() { return Object.keys(vaultMap); },
    state() { return 'ready'; },
});

(async () => {
BODY
})().catch(error => {
    console.error(error.stack || error);
    process.exitCode = 1;
});
""".replace("BODY", body)
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def run_actions_matchers_js(body):
    """Execute the option-menu matcher slice of actions.js with Node."""
    harness = r"""
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('js/actions.js', 'utf8');
const start = source.indexOf('const _OPT_LOOKBACK');
const end = source.indexOf('function _lastNonEmptyLine');
if (start < 0 || end < 0 || end <= start) throw new Error('matcher slice not found');
const matcherBody = MATCHER_BODY;
vm.runInThisContext(source.slice(start, end) + '\n' + matcherBody, {
    filename: 'js/actions.js#option-matchers',
});
""".replace("MATCHER_BODY", json.dumps(body))
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class VaultClientContractTests(unittest.TestCase):
    def test_option_matchers_match_server_acceptance_without_widening(self):
        result = run_actions_matchers_js(r"""
const selectedLines = [
    'Choose an action',
    '  ❯ YES, continue with this choice',
    'status detail without a No sibling',
    'Press enter to continue',
];
const outsideRegion = [
    '❯ Yes, stale selection',
    '────────────',
    '1. Retry',
    '2. Cancel',
    'Press enter to confirm',
];
const staleFooter = ['❯ Yes', 'Press enter to confirm'];
for (let i = 0; i < 31; i++) staleFooter.push('status ' + i);

console.log(JSON.stringify({
    confirmFooter: _OPT_FOOTER_RE.test('Press enter to confirm'),
    continueFooter: _OPT_FOOTER_RE.test('Press enter to continue'),
    widerFooter: _OPT_FOOTER_RE.test('Press enter to proceed'),
    selected: _isSelectedYesMenu(selectedLines, _findOptionFooter(selectedLines)),
    yesPrefix: _isSelectedYesMenu(
        ['❯ Yesterday', 'Press enter to confirm'],
        _findOptionFooter(['❯ Yesterday', 'Press enter to confirm'])
    ),
    outsideRegion: _isSelectedYesMenu(outsideRegion, _findOptionFooter(outsideRegion)),
    staleFooter: _isSelectedYesMenu(staleFooter, _findOptionFooter(staleFooter)),
}));
""")
        self.assertTrue(result["confirmFooter"])
        self.assertTrue(result["continueFooter"])
        self.assertFalse(result["widerFooter"])
        self.assertTrue(result["selected"])
        self.assertFalse(result["yesPrefix"])
        self.assertFalse(result["outsideRegion"])
        self.assertFalse(result["staleFooter"])

    def test_vault_exposes_presence_and_state_without_loading_values(self):
        vault = source("js/vault.js")
        for method in ("load", "save", "available", "has", "handles", "state"):
            self.assertIn(method + ": function", vault)
        self.assertIn("function vaultSetAdapter(adapter)", vault)
        for operation in (
            "vaultState",
            "vaultHas",
            "vaultGet",
            "vaultPut",
            "vaultForget",
            "vaultForgetAll",
            "vaultHandles",
            "vaultScan",
            "vaultResolve",
        ):
            self.assertIn("function " + operation + "(", vault)

        # The composer and chip UI know the facade, never localStorage or the
        # concrete adapter. A storage swap therefore stays inside vault.js.
        self.assertNotIn("localStorage", source("js/input.js"))
        self.assertNotIn("localStorage", source("js/segments.js"))
        self.assertNotIn("_vaultAdapter", source("js/input.js"))
        self.assertNotIn("_vaultAdapter", source("js/segments.js"))

    def test_vault_scan_uses_presence_metadata_without_loading_values(self):
        result = run_vault_js(r"""
let loads = 0;
vaultSetAdapter({
    load() { loads += 1; return {sudo: 'test-secret'}; },
    save() {},
    available() { return true; },
    has(handle) { return handle === 'sudo'; },
    handles() { return ['sudo']; },
    state() { return 'ready'; },
});
const first = vaultScan('[$sudo]');
const second = vaultScan('again [$sudo]');
const loadsBeforeResolve = loads;
const resolved = vaultResolve('[$sudo]');
console.log(JSON.stringify({
    loadsBeforeResolve,
    loadsAfterResolve: loads,
    first,
    second,
    resolvedText: resolved.text,
}));
""")
        self.assertEqual(result["loadsBeforeResolve"], 0)
        self.assertEqual(result["loadsAfterResolve"], 1)
        self.assertEqual(result["first"]["state"], "ready")
        self.assertEqual(result["first"]["used"], ["sudo"])
        self.assertEqual(result["second"]["used"], ["sudo"])
        self.assertEqual(result["resolvedText"], "test-secret")

    def test_local_storage_adapter_hydrates_once_for_repeated_scans(self):
        result = run_vault_js(r"""
let reads = 0;
const stored = JSON.stringify({sudo: 'test-secret'});
globalThis.localStorage = {
    getItem(key) {
        if (key === VAULT_STORAGE_KEY) reads += 1;
        return key === VAULT_STORAGE_KEY ? stored : null;
    },
    setItem() {},
    removeItem() {},
};
localStorageVaultAdapter.invalidate();
vaultSetAdapter(localStorageVaultAdapter);
const first = vaultScan('[$sudo]');
const second = vaultScan('[$sudo] again');
console.log(JSON.stringify({reads, first, second}));
""")
        self.assertEqual(result["reads"], 1)
        self.assertEqual(result["first"]["used"], ["sudo"])
        self.assertEqual(result["second"]["used"], ["sudo"])

    def test_locked_and_unavailable_vault_states_are_not_rendered_as_empty(self):
        vault = source("js/vault.js")
        segments = source("js/segments.js")
        settings = source("js/settings.js")
        self.assertIn("state === 'ready' || state === 'locked'", vault)
        self.assertIn("vaultResult.state !== 'ready'", segments)
        self.assertIn("Vault is locked.", segments)
        self.assertIn("Browser storage is unavailable.", segments)
        self.assertIn("Vault locked. Unlock it to list entries.", settings)
        self.assertIn("Vault storage unavailable in this browser.", settings)

    def test_client_token_grammar_has_no_lookbehind_and_checks_escape_by_hand(self):
        vault = source("js/vault.js")
        self.assertIn(
            "const VAULT_TOKEN_RE = /\\[\\$([a-z0-9][a-z0-9._-]{1,31})\\]/g;",
            vault,
        )
        self.assertNotIn("(?<", vault)
        self.assertIn("source[offset - 1] === '\\\\'", vault)

    def test_composer_resolution_atomically_forces_secret_and_disables_expansion(self):
        do_paste = between(
            source("js/input.js"), "async function doPaste()", "function triggerUpload()"
        )
        assembled = do_paste.index("let finalText = raw")
        resolved = do_paste.index("const vaultResult = vaultResolve(finalText)")
        request = do_paste.index("const resp = await fetch('/type'", resolved)
        self.assertLess(assembled, resolved)
        self.assertLess(resolved, request)
        self.assertIn("[secret, expand] = [true, false]", do_paste)
        self.assertIn("expand: expand", do_paste)
        self.assertIn("secret: secret", do_paste)

    def test_composer_vault_branches_fail_open_when_module_is_missing(self):
        do_paste = between(
            source("js/input.js"), "async function doPaste()", "function triggerUpload()"
        )
        self.assertNotIn("VAULT_TOKEN_RE", do_paste)
        self.assertIn(
            "if (hasAttachments && typeof vaultScan === 'function')", do_paste
        )
        self.assertIn(
            "else if (!hasAttachments && typeof vaultResolve === 'function')",
            do_paste,
        )
        self.assertIn("Vault token sent literally", do_paste)

    def test_chip_renderer_scans_handles_without_resolving_values(self):
        segments = source("js/segments.js")
        renderer = between(segments, "function renderSegChips", "// --- sheet ---")
        self.assertIn("vaultScan(input.value)", renderer)
        self.assertNotIn("vaultResolve", renderer)

    def test_sheet_clears_password_field_before_closing(self):
        segments = source("js/segments.js")
        save = between(
            segments, "function vaultSaveFromSheet", "function vaultForgetFromSheet"
        )
        cleared = save.index("valueEl.value = ''")
        closed = save.index("segSheetClose()")
        self.assertLess(cleared, closed)

    def test_failure_restores_only_token_text_and_never_schedules_a_secret_draft(self):
        do_paste = between(
            source("js/input.js"), "async function doPaste()", "function triggerUpload()"
        )
        self.assertGreaterEqual(do_paste.count("input.value = raw"), 2)
        self.assertNotIn("input.value = finalText", do_paste)
        self.assertEqual(do_paste.count("!secret && typeof saveDraftSoon"), 2)

    def test_both_failed_send_paths_rerender_chips_after_restoring_token_text(self):
        do_paste = between(
            source("js/input.js"), "async function doPaste()", "function triggerUpload()"
        )
        response_failure = between(
            do_paste, "} else {\n            showFlash('error'", "    } catch (e) {"
        )
        offline_failure = between(
            do_paste, "    } catch (e) {", "    } finally {"
        )
        rerender = "if (typeof renderSegChips === 'function') renderSegChips();"

        for branch in (response_failure, offline_failure):
            restored = branch.index("input.value = raw")
            rerendered = branch.index(rerender)
            self.assertLess(restored, rerendered)
            self.assertEqual(branch.count(rerender), 1)

    def test_vault_chip_sheet_is_local_only_and_never_reads_the_value(self):
        segments = source("js/segments.js")
        vault_sheet = between(segments, "function vaultPreview", "function segPreview")
        self.assertNotIn("fetch(", vault_sheet)
        self.assertNotIn("vaultGet(", vault_sheet)
        self.assertIn("Stored in this browser, in plaintext", vault_sheet)
        self.assertIn("type=\"password\"", vault_sheet)
        self.assertIn(">Forget</button>", vault_sheet)

    def test_will_send_preview_posts_only_the_unresolved_composer_text(self):
        segments = source("js/segments.js")
        preview = between(segments, "async function segPreviewAll", "function segEdit")
        self.assertIn("fetch('/segments/expand'", preview)
        self.assertIn("JSON.stringify({text: input.value})", preview)
        self.assertNotIn("vaultResolve", preview)
        self.assertIn("vaultScan(input.value)", preview)
        self.assertIn("SEGMENT PREVIEW", preview)
        self.assertIn("Vault tokens stay masked here", preview)

    def test_quick_send_is_handle_bound_same_pane_secret_and_visibly_named(self):
        vault = source("js/vault.js")
        render = between(vault, "function renderVaultQuickSend", "async function vaultSendDefault")
        send = vault[vault.index("async function vaultSendDefault") :]
        self.assertIn("vaultPromptHandle(_termLatestContent)", render)
        self.assertIn("VAULT_SUDO_PROMPT_RE", vault)
        self.assertNotIn("_isPasswordPrompt", render)
        self.assertIn("getInputTarget() === _termTarget", render)
        self.assertNotIn("VAULT_QUICK_SEND_ENABLED", render)
        self.assertIn("btn.textContent = '$' + handle", render)
        self.assertIn("getInputTarget() !== _termTarget", send)
        self.assertNotIn("_isPasswordPrompt", send)
        self.assertIn("target: _termTarget", send)
        self.assertIn("fetch('/type'", send)
        self.assertIn("text: value", send)
        self.assertIn("expand: false", send)
        self.assertIn("secret: true", send)
        self.assertNotIn("input.value", send)
        self.assertIn("Password sent", send)

    def test_quick_send_rejects_ssh_prompt_and_diverging_destination(self):
        result = run_vault_js(r"""
_termLatestContent = "deploy@vps's password:";
renderVaultQuickSend();
const sshHidden = button.hidden;
await vaultSendDefault();
const sshFetchCount = fetchCalls.length;

_termLatestContent = '[sudo] password for user:';
inputTarget = _termTarget;
renderVaultQuickSend();
const sudoHidden = button.hidden;
const sudoLabel = button.textContent;

inputTarget = 'vault-split:0.1';
renderVaultQuickSend();
const splitHidden = button.hidden;
await vaultSendDefault();
const splitFetchCount = fetchCalls.length;

inputTarget = _termTarget;
renderVaultQuickSend();
await vaultSendDefault();
console.log(JSON.stringify({
    sshHidden,
    sshFetchCount,
    sudoHidden,
    sudoLabel,
    splitHidden,
    splitFetchCount,
    fetchCalls,
}));
""")
        self.assertTrue(result["sshHidden"])
        self.assertEqual(result["sshFetchCount"], 0)
        self.assertFalse(result["sudoHidden"])
        self.assertEqual(result["sudoLabel"], "$sudo")
        self.assertTrue(result["splitHidden"])
        self.assertEqual(result["splitFetchCount"], 0)
        self.assertEqual(len(result["fetchCalls"]), 1)
        self.assertEqual(
            result["fetchCalls"][0]["body"],
            {
                "text": "test-secret",
                "enter": True,
                "expand": False,
                "secret": True,
                "target": "vault-main:0.0",
            },
        )

    def test_quick_send_handle_label_keeps_a_compact_visible_width(self):
        button_css = between(source("css/input.css"), ".btn-vault {", ".btn-vault.hidden")
        self.assertIn("min-width: 54px", button_css)
        self.assertIn("text-transform: none", button_css)

    def test_missing_vault_handle_uses_literal_posture_not_password_sent(self):
        do_paste = between(
            source("js/input.js"), "async function doPaste()", "function triggerUpload()"
        )
        resolution = between(
            do_paste,
            "else if (!hasAttachments && typeof vaultResolve === 'function')",
            "    // Step 3: send combined text",
        )
        self.assertIn("vaultResult.missing.length", resolution)
        self.assertIn("vaultSentLiterally = true", resolution)
        self.assertIn("vaultLiteralNotice", resolution)
        self.assertLess(
            do_paste.index("if (vaultSentLiterally)"),
            do_paste.index("secret ? 'Password sent'"),
        )

    def test_composer_documents_the_benign_main_to_split_overclassification(self):
        do_paste = between(
            source("js/input.js"), "async function doPaste()", "function triggerUpload()"
        )
        self.assertIn("legacy detector reads the main pane", do_paste)
        self.assertIn("never selects and injects a stored value", do_paste)

    def test_settings_show_presence_and_explicit_forget_controls_without_values(self):
        settings = source("js/settings.js")
        vault_settings = between(settings, "function _renderVaultSettings", "function _renderToggle")
        self.assertIn("in plaintext", vault_settings)
        self.assertIn("stored.textContent = 'stored'", vault_settings)
        self.assertIn("forget.textContent = 'Forget'", vault_settings)
        self.assertIn("Forget all vault entries", vault_settings)
        self.assertNotIn("vaultGet(", vault_settings)

    def test_vault_script_loads_before_composer_and_segment_code(self):
        html = source("index.html")
        vault_pos = html.index('/js/vault.js')
        self.assertLess(vault_pos, html.index('/js/input.js'))
        self.assertLess(vault_pos, html.index('/js/segments.js'))

    def test_service_worker_cache_matches_index_for_tracked_assets(self):
        html = source("index.html")
        worker = source("sw.js")
        static_urls = between(worker, "const STATIC_URLS = [", "];")
        index_assets = set(re.findall(r'(?:href|src)="([^"]+)"', html))
        cached_assets = set(re.findall(r"'([^']+)'", static_urls))

        for cached in cached_assets - {'/'}:
            self.assertIn(cached, index_assets)

        # Pin the RELATIONSHIP, not the version number. A guard that hardcodes
        # `?v=1` fails on the next legitimate bump, and a guard that fails for a
        # correct change teaches people to edit the test instead of the code.
        vault_entries = [u for u in cached_assets if u.split('?')[0] == '/js/vault.js']
        self.assertEqual(
            len(vault_entries), 1,
            "sw.js must precache exactly one /js/vault.js entry: input.js reaches "
            "for the vault on the send path, so an offline shell without it is a "
            "shell that can throw",
        )

    def test_service_worker_cache_generation_moved_when_the_vault_landed(self):
        """The shell generation must not still be the pre-vault one.

        Bumping VERSION is what evicts the previous STATIC_CACHE; leaving it
        alone keeps stale entries alive forever. Asserted against the value this
        repo carried before the vault rather than against whatever is current,
        so an ordinary later bump does not fail this.
        """
        worker = source("sw.js")
        match = re.search(r"const VERSION = '([^']+)';", worker)
        self.assertIsNotNone(match, "sw.js has no VERSION")
        self.assertNotEqual(match.group(1), 'assist-v3-031')

    def test_type_route_has_no_payload_logging(self):
        route = source("routes/input.py")
        type_route = between(route, 'def type_text():', '_UPLOAD_CHUNK =')
        self.assertNotIn("print(", type_route)
        self.assertNotIn("logger.", type_route)
        self.assertNotIn("log.", type_route)

    def test_security_posture_calls_browser_storage_plaintext(self):
        posture = source("SECURITY.md")
        self.assertIn("Secret vault: plaintext in this browser", posture)
        self.assertIn("Those values are plaintext", posture)
        self.assertIn("steals only an Assist auth cookie", posture)
        self.assertIn("unlocked browser", posture)
        self.assertIn("crypto.subtle", posture)
        self.assertIn("secure context", posture)
        self.assertIn("scheme, host and port", posture)
        self.assertIn("does not migrate", posture)
        self.assertIn("without possessing the device", posture)

    def test_plain_http_service_worker_limit_is_documented_at_registration_boundary(self):
        worker = source("sw.js")
        self.assertIn("secure context", worker)
        self.assertIn("plain-HTTP phone", worker)


if __name__ == "__main__":
    unittest.main()
