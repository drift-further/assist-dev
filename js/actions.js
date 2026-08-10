// actions.js — Smart action detection + rendering (SMART_PATTERNS)

// Auto-Yes: now server-side. UI just toggles and shows countdown.
let _autoyesState = {};  // session -> bool (mirrors server state)
let _autoyesDelays = {};  // session -> seconds (per-session active delay, mirrors server)
let _autoyesCountdown = null;  // { target, remaining, delay, prompt_type } or null
let _autoyesDelay = (SETTINGS && SETTINGS.autoyes) ? SETTINGS.autoyes.default_delay : 5;  // current delay setting (persists across toggles)
let _autoyesPickerVisible = false;

function isAutoYes(session) {
    return !!_autoyesState[session];
}

// Show inline delay picker instead of browser prompt()
// When OFF: picker enables auto-yes ("Start").
// When ON: picker edits the live delay ("Update") or turns it off ("Off").
function showAutoYesPicker() {
    const target = _smartActionTarget || _termTarget;
    const session = target ? target.split(':')[0] : '';
    if (!session) return;

    const picker = document.getElementById('autoyes-picker');
    if (!picker) return;
    const on = isAutoYes(session);
    // Prefill with the session's active delay when running, else last-used value
    if (on && _autoyesDelays[session]) _autoyesDelay = _autoyesDelays[session];
    _renderAyPickVal();
    const goBtn = document.getElementById('ay-pick-go');
    if (goBtn) goBtn.textContent = on ? 'Update' : 'Start';
    const offBtn = document.getElementById('ay-pick-off');
    if (offBtn) offBtn.style.display = on ? '' : 'none';
    const labelEl = picker.querySelector('.ay-pick-label');
    if (labelEl) labelEl.textContent = on ? 'Change delay' : 'Auto-Yes delay';
    picker.classList.add('visible');
    _autoyesPickerVisible = true;
}

// Step by 1s at/above 1s, by 100ms below it (min 100ms, max 30s).
function ayPickAdjust(delta) {
    let v = _autoyesDelay;
    if (delta < 0) v -= (v <= 1) ? 0.1 : 1;
    else           v += (v < 1) ? 0.1 : 1;
    _autoyesDelay = Math.max(0.1, Math.min(30, Math.round(v * 10) / 10));
    _renderAyPickVal();
}

// Render the picker value + unit: seconds at/above 1s, milliseconds below.
function _renderAyPickVal() {
    const valEl = document.getElementById('ay-pick-val');
    const unitEl = document.querySelector('#autoyes-picker .ay-pick-unit');
    if (valEl) valEl.textContent = _autoyesDelay < 1
        ? String(Math.round(_autoyesDelay * 1000))
        : String(_autoyesDelay);
    if (unitEl) unitEl.textContent = _autoyesDelay < 1 ? 'ms' : 'sec';
}

// Format a delay (seconds, possibly fractional) for flash messages.
function _fmtDelay(d) {
    return d < 1 ? `${Math.round(d * 1000)}ms` : `${d}s`;
}

function ayPickConfirm() {
    const target = _smartActionTarget || _termTarget;
    const session = target ? target.split(':')[0] : '';
    document.getElementById('autoyes-picker').classList.remove('visible');
    _autoyesPickerVisible = false;
    if (!session) return;
    if (isAutoYes(session)) {
        _setAutoYesDelay(session, _autoyesDelay);  // already running — update delay
    } else {
        _enableAutoYes(session, _autoyesDelay);     // off — enable with chosen delay
    }
}

function ayPickTurnOff() {
    const target = _smartActionTarget || _termTarget;
    const session = target ? target.split(':')[0] : '';
    document.getElementById('autoyes-picker').classList.remove('visible');
    _autoyesPickerVisible = false;
    if (session) _enableAutoYes(session, null);  // toggle off
}

function ayPickCancel() {
    document.getElementById('autoyes-picker').classList.remove('visible');
    _autoyesPickerVisible = false;
}

// Update the delay for an already-running session (no toggle off/on)
async function _setAutoYesDelay(session, delay) {
    try {
        const resp = await fetch('/autoyes/set-delay', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session, delay }),
        });
        const data = await resp.json();
        if (data.ok) {
            _autoyesDelays[session] = data.delay;
            showFlash('sent', `Auto-Yes delay → ${_fmtDelay(data.delay)}`);
        } else {
            showFlash('error', data.error || 'Failed to update');
        }
    } catch (e) {
        showFlash('error', 'Failed to update');
    }
}

async function _enableAutoYes(session, delay) {
    try {
        const body = { session };
        if (delay !== null) body.delay = delay;
        const resp = await fetch('/autoyes/toggle', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.ok) {
            _autoyesState[session] = data.enabled;
            if (data.enabled && delay !== null) _autoyesDelays[session] = delay;
            else if (!data.enabled) delete _autoyesDelays[session];
            showFlash('sent', data.enabled ? `Auto-Yes ON (${_fmtDelay(delay)})` : 'Auto-Yes OFF');
            updateAutoYesUI(session);
            _getSmartState(_termTarget).key = '';  // force re-render (toggle label changed)
            if (_termLatestContent) {
                const info = _paneInfo[_termTarget];
                const detected = detectSmartActions(
                    stripAnsi(_termLatestContent),
                    _termTarget,
                    info && info.agent_kind
                );
                renderSmartActions(detected);
            }
        }
    } catch (e) {
        showFlash('error', 'Failed to toggle');
    }
}

// Legacy name — called from +menu and inline toggle
async function toggleAutoYes() {
    showAutoYesPicker();
}

function updateAutoYesUI(session) {
    if (!session && _termTarget) session = _termTarget.split(':')[0];
    const btn = document.getElementById('btn-autoyes');
    if (btn) {
        const active = session ? isAutoYes(session) : false;
        btn.classList.toggle('active', active);
        btn.textContent = active ? '\u26A1 Auto-Yes' : 'Auto-Yes';
    }
    // Arming/disarming is the only moment the bar's slot may change size, and
    // it happens on a deliberate tap rather than mid-prompt.
    _syncCountdownReservation();
}

// Sync auto-yes state from server on load / tab switch
async function syncAutoYesState() {
    try {
        const resp = await fetch('/autoyes/status');
        const data = await resp.json();
        _autoyesState = data.sessions || {};
        _autoyesDelays = data.delays || {};
        // Update countdown if any
        const target = _smartActionTarget || _termTarget;
        if (target && data.countdowns && data.countdowns[target]) {
            const cd = data.countdowns[target];
            _autoyesCountdown = { target, remaining: cd.remaining, delay: cd.delay || 5, prompt_type: cd.prompt_type, summary: cd.summary || null };
        } else {
            _autoyesCountdown = null;
        }
    } catch(e) {}
}

async function cancelAutoYesCountdown() {
    if (!_autoyesCountdown) return;
    try {
        await fetch('/autoyes/cancel', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ target: _autoyesCountdown.target }),
        });
        _autoyesCountdown = null;
        _renderAutoYesCountdown();
        showFlash('sent', 'Cancelled');
    } catch(e) {}
}

// Handle autoyes WS messages from server
function handleAutoYesWsMessage(msg) {
    if (msg.event === 'countdown') {
        _autoyesCountdown = { target: msg.target, remaining: msg.remaining, delay: msg.delay || 5, prompt_type: msg.prompt_type, summary: msg.summary || null };
        _renderAutoYesCountdown();
    } else if (msg.event === 'fired') {
        _autoyesCountdown = null;
        _renderAutoYesCountdown();
        showFlash('sent', 'Auto-Yes sent');
    } else if (msg.event === 'cancelled') {
        _autoyesCountdown = null;
        _renderAutoYesCountdown();
    }
}

// Hold the countdown bar's slot open whenever Auto-Yes is armed for the session
// on screen. Without it the bar appearing mid-prompt lifts the smart-action
// buttons by its own height, and a tap already in flight lands on the wrong
// option. See .autoyes-countdown.reserved in css/terminal.css.
function _syncCountdownReservation() {
    const bar = document.getElementById('autoyes-countdown');
    if (!bar) return;
    const session = (_smartActionTarget || _termTarget || '').split(':')[0];
    const armed = !!session && typeof isAutoYes === 'function' && isAutoYes(session);
    bar.classList.toggle('reserved', armed);
}

let _countdownTimer = null;
function _renderAutoYesCountdown() {
    const bar = document.getElementById('autoyes-countdown');
    if (!bar) return;

    if (_countdownTimer) { clearInterval(_countdownTimer); _countdownTimer = null; }
    _syncCountdownReservation();

    if (!_autoyesCountdown) {
        bar.classList.remove('visible');
        return;
    }

    const totalDelay = _autoyesCountdown.delay || 5;
    const summary = _autoyesCountdown.summary || '';
    const deadline = Date.now() + _autoyesCountdown.remaining * 1000;
    bar.classList.add('visible');

    // Set summary text once
    const sumEl = bar.querySelector('.ay-summary');
    if (sumEl) {
        const short = summary.length > 50 ? summary.substring(0, 50) + '\u2026' : summary;
        sumEl.textContent = short;
        sumEl.title = summary;
    }

    const update = () => {
        const leftSec = Math.max(0, (deadline - Date.now()) / 1000);
        const wholeSeconds = Math.ceil(leftSec);
        const label = bar.querySelector('.ay-label');
        const progress = bar.querySelector('.ay-progress');
        if (label) label.textContent = wholeSeconds > 0 ? String(wholeSeconds) : '0';
        if (progress) progress.style.width = `${(1 - leftSec / totalDelay) * 100}%`;
        if (leftSec <= 0) {
            clearInterval(_countdownTimer);
            _countdownTimer = null;
            bar.classList.remove('visible');
        }
    };
    update();
    _countdownTimer = setInterval(update, 100);
}

// Check if a detected result qualifies for auto-yes.
function _isAutoYesCandidate(result) {
    if (!result) return false;
    if (result.id === 'permission-yna' || result.id === 'confirm-yn' ||
        result.id === 'opencode-permission' || result.id === 'cursor-permission' ||
        result.id === 'cursor-trust' || result.id === 'package-confirm' ||
        result.id === 'ssh-host-key' || result.id === 'selected-yes') return true;
    if (result.id !== 'numbered-options') return false;
    const first = result.actions[0];
    return first && first.isOption && /^1\.\s*Yes/i.test(first.label);
}

// --- Numbered-option region bounds (shared by match + getActions) ---------
// The option block sits between a ──── separator and the menu footer. Claude
// Code's AskUserQuestion menu draws a SECOND separator between the last real
// option and the trailing "Chat about this" row, so the separator NEAREST the
// footer leaves a single option in the region: the two-option gate failed, no
// smart actions rendered, and the tab never got its ? badge on a question that
// was genuinely waiting. Walk up through separators and take the first region
// holding at least two options.
// Mirrors _option_region_start() in routes/autoyes.py.
const _OPT_LOOKBACK = 60;
const _OPT_RE = /^\s*(?:[^\d\s]\s*)?(\d+)[\.\)]\s+\S/;
const _OPT_TEXT_RE = /^\s*(?:[^\d\s]\s*)?(\d+)[\.\)]\s+(.+)/;
const _OPT_SEP_RE = /^[\s]*─{10,}/;
const _OPT_FOOTER_RE = /(?:Enter to select|Esc to cancel|Navigate)\s*[·•]|Press enter to confirm/;

function _optionCount(lines, startIdx, endIdx) {
    let count = 0;
    for (let i = startIdx; i < endIdx; i++) {
        if (_OPT_RE.test(lines[i])) count++;
    }
    return count;
}

function _findOptionFooter(lines) {
    for (let i = lines.length - 1; i >= 0; i--) {
        if (_OPT_FOOTER_RE.test(lines[i])) return i;
    }
    return -1;
}

// An internal divider inside the option block is what tells a long-form
// AskUserQuestion apart from a Yes/No permission gate — the question splits its
// "Chat about this" escape hatch off with a second ──── rule. Its options are
// prose you read in the pane, so it marks the tab and notifies but renders no
// button wall (five full-width buttons duplicate the pane and bury it).
function _hasInternalDivider(lines, startIdx, endIdx) {
    for (let i = startIdx; i < endIdx; i++) {
        if (_OPT_SEP_RE.test(lines[i])) return true;
    }
    return false;
}

function _optionRegionStart(lines, footerIdx) {
    const floor = Math.max(0, footerIdx - _OPT_LOOKBACK);
    for (let i = footerIdx - 1; i >= floor; i--) {
        if (!_OPT_SEP_RE.test(lines[i])) continue;
        // A separator that leaves fewer than two options below it is an
        // internal divider, not the top of the block — keep walking up.
        if (_optionCount(lines, i + 1, footerIdx) >= 2) return i + 1;
    }
    return floor;
}

// Mirrors routes/autoyes.py:_SELECTED_YES_RE. The arrow row alone is not a
// liveness signal: require the enclosing divider, a sibling No row, and the
// live menu footer in one compact block.
function _isSelectedYesMenu(lines, footerIdx) {
    if (footerIdx < 0 || (lines.length - 1 - footerIdx) > 30) return false;
    const floor = Math.max(0, footerIdx - 8);
    let dividerIdx = -1;
    let selectedIdx = -1;
    let noIdx = -1;
    for (let i = floor; i < footerIdx; i++) {
        if (_OPT_SEP_RE.test(lines[i])) dividerIdx = i;
        if (/^\s*❯\s+Yes\s*$/.test(lines[i])) selectedIdx = i;
        if (/^\s+No\s*$/.test(lines[i])) noIdx = i;
    }
    return dividerIdx >= floor && dividerIdx < selectedIdx &&
        noIdx > selectedIdx && noIdx - selectedIdx <= 3 && footerIdx - noIdx <= 3;
}

function _lastNonEmptyLine(tail) {
    const lines = tail.split('\n');
    for (let i = lines.length - 1; i >= 0; i--) {
        if (lines[i].trim()) return lines[i];
    }
    return '';
}

const SMART_PATTERNS = [
    {
        id: 'claude-resume',
        desc: 'Resume Claude session',
        // PASSIVE: offer the buttons, but never mark the tab or notify.
        //
        // Claude Code prints "Resume this session with: claude --resume <uuid>"
        // as its FAREWELL and drops back to a shell. That text then sits in the
        // pane's last 6 lines forever, so this pattern used to badge the tab as
        // needing input permanently — measured 2026-08-10 at 8 of 18 live panes
        // continuously flagged, every one of them an exited session with nothing
        // waiting on anybody. It is the dominant source of gotcha 528's "reaction
        // patterns fire on panes that are not waiting on anything".
        //
        // The offer itself is still useful when you are looking at the pane, so
        // this is not a deletion: `passive` keeps the action bar and drops only
        // the pending-question signalling. Distinct from notifyOnly, which is
        // the opposite trade (mark and notify, but draw no bar).
        passive: true,
        // Proper UUID: 8-4-4-4-12 hex, and must be in last 6 lines (not stale scrollback)
        _uuidRe: /claude\s+--resume\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/,
        match: function(tail) {
            const bottom = tail.split('\n').slice(-6).join('\n');
            return this._uuidRe.test(bottom);
        },
        getActions: function(tail) {
            const bottom = tail.split('\n').slice(-6).join('\n');
            const m = bottom.match(this._uuidRe);
            if (!m) return null;
            return [
                { label: 'Resume session', claudeCmd: CLAUDE_CMD + ' --resume ' + m[1], restart: true, color: 'green' },
                { label: 'New session', claudeCmd: CLAUDE_CMD, restart: true, color: 'cyan' },
            ];
        }
    },
    {
        id: 'permission-yna',
        desc: 'Permission prompt',
        agents: ['claude'],
        match: (tail) => {
            // Only check last 8 lines — avoids false positives from answered prompts in scrollback
            const bottom = tail.split('\n').slice(-8).join('\n');
            return /\(y\/n\/a\)/i.test(bottom) ||
                   /\[Y\/n\/a\]/i.test(bottom) ||
                   /Allow once.*Always allow.*Deny/i.test(bottom) ||
                   /Yes.*\(y\).*Always.*\(a\).*No.*\(n\)/i.test(bottom);
        },
        actions: [
            { label: 'Allow (y)', send: 'y', enter: false, color: 'green' },
            { label: 'Always (a)', send: 'a', enter: false, color: 'cyan' },
            { label: 'Deny (n)', send: 'n', enter: false, color: 'red' },
        ]
    },
    {
        id: 'opencode-permission',
        desc: 'OpenCode permission',
        agents: ['opencode'],
        // OpenCode TUI dialog (verified on 1.18.4): opens with "Allow once"
        // selected; Left/Right cycle the selection WITH WRAPAROUND and Enter
        // confirms. y/n keys (and Tab) do nothing — answers are key sequences.
        // Mirrors routes/autoyes.py:_OPENCODE_PERMISSION_RE.
        match: (tail) => {
            const bottom = tail.split('\n').slice(-8).join('\n');
            return /Allow once\s+Allow always\s+Reject/.test(bottom) &&
                   /Permission required/.test(tail);
        },
        actions: [
            { label: 'Allow once', send: '', enter: true, color: 'green' },
            { label: 'Allow always', keys: ['Right'], enter: true, color: 'cyan' },
            { label: 'Reject', keys: ['Left'], enter: true, color: 'red' },
        ]
    },
    {
        id: 'cursor-permission',
        desc: 'Cursor: run command',
        agents: ['cursor'],
        // Cursor CLI shell approval (verified live on cursor-agent
        // 2026.07.23-e383d2b). Its TUI gates only two things — this and the
        // workspace-trust dialog below; file edits/writes are never gated.
        //   Run this command?
        //   Not in allowlist: cat, head
        //    → Run (once) (y)
        //      Add Shell(cat), Shell(head) to allowlist? (tab)
        //      Run Everything (shift+tab)
        //      Skip & tell the agent what to do instead (esc or n)
        // The dialog is 8-11 rows tall and an optional "ctrl+r to review
        // changed files" hint can sit BELOW it, so the window gets a fixed
        // 14-line floor instead of the 8 used elsewhere. Requiring the header
        // and the option row together rules out a match on displayed text.
        // Mirrors routes/autoyes.py:_CURSOR_PERMISSION_*.
        match: (tail) => {
            const bottom = tail.split('\n').slice(-14).join('\n');
            return /Run this command\?/.test(bottom) &&
                   /Run \(once\)\s*\(y\)/.test(bottom);
        },
        actions: [
            { label: 'Run once (y)', send: 'y', enter: false, color: 'green' },
            { label: 'Allowlist (tab)', keys: ['Tab'], enter: false, color: 'cyan' },
            { label: 'Run all (⇧tab)', keys: ['shift+Tab'], enter: false, color: 'amber' },
            { label: 'Skip (n)', send: 'n', enter: false, color: 'red' },
        ]
    },
    {
        id: 'cursor-trust',
        desc: 'Cursor: trust workspace',
        agents: ['cursor'],
        // Cursor's launch gate. Unlike every other prompt handled here it is
        // NOT erased when answered — the box stays on screen with the ▶ marker
        // gone and the footer replaced by "⏳ Trusting workspace…". The footer
        // is therefore the liveness signal: matching the "[a] Trust this
        // workspace" row alone would keep matching after the answer and fire a
        // stray "a" into the TUI's input box.
        // Mirrors routes/autoyes.py:_CURSOR_TRUST_*.
        match: (tail) => {
            const bottom = tail.split('\n').slice(-8).join('\n');
            return /\[a\] Trust this workspace/.test(bottom) &&
                   /Use arrow keys to navigate/.test(bottom);
        },
        actions: [
            { label: 'Trust (a)', send: 'a', enter: false, color: 'green' },
            { label: 'Quit (q)', send: 'q', enter: false, color: 'red' },
        ]
    },
    {
        id: 'sudo-password',
        desc: 'Sudo password',
        match: (tail) => /\[sudo\] password for [^:]+:\s*$/i.test(_lastNonEmptyLine(tail)),
        actions: [
            { label: 'Send stored password', sudo: true, secret: true, color: 'amber' },
        ]
    },
    {
        id: 'package-confirm',
        desc: 'Package manager confirmation',
        // Package and ssh prompts are shell-layer reactions: keep them
        // eligible inside agent panes, but require the last non-empty line.
        // Mirrors routes/autoyes.py:_PACKAGE_CONFIRM_RE.
        match: (tail) => /(?:Do you want to continue\?\s*\[Y\/n\]|Is this ok\s*\[y\/N\]:?)\s*$/i.test(_lastNonEmptyLine(tail)),
        actions: [
            { label: 'Yes (y)', send: 'y', enter: true, color: 'green' },
            { label: 'No (n)', send: 'n', enter: true, color: 'red' },
        ]
    },
    {
        id: 'ssh-host-key',
        desc: 'SSH host key',
        // Mirrors routes/autoyes.py:_SSH_HOST_KEY_RE.
        match: (tail) => /Are you sure you want to continue connecting \(yes\/no(?:\/\[fingerprint\])?\)\?\s*$/i.test(_lastNonEmptyLine(tail)),
        actions: [
            { label: 'Continue (yes)', send: 'yes', enter: true, color: 'green' },
            { label: 'Cancel (no)', send: 'no', enter: true, color: 'red' },
        ]
    },
    {
        id: 'confirm-yn',
        desc: 'Confirmation',
        match: (tail) => {
            // Mirrors routes/autoyes.py: a displayed or echoed form is not an
            // interactive prompt unless it is the last non-empty line.
            const line = _lastNonEmptyLine(tail);
            if (/\(y\/n\/a\)/i.test(line) || /\[Y\/n\/a\]/i.test(line)) return false;
            return line.match(/\(y\/n\)|\[Y\/n\]|\[y\/N\]|\(yes\/no\)/i);
        },
        getActions: (_tail, match) => {
            const withEnter = /\[Y\/n\]|\[y\/N\]|\(yes\/no\)/i.test(match[0]);
            return [
                { label: 'Yes (y)', send: 'y', enter: withEnter, color: 'green' },
                { label: 'No (n)', send: 'n', enter: withEnter, color: 'red' },
            ];
        }
    },
    {
        id: 'selected-yes',
        desc: 'Confirm selected option',
        agents: ['claude', 'codex', 'gemini'],
        match: (tail) => {
            const lines = tail.split('\n');
            return _isSelectedYesMenu(lines, _findOptionFooter(lines));
        },
        actions: [
            { label: 'Confirm Yes', send: '', enter: true, color: 'green' },
        ]
    },
    {
        id: 'numbered-options',
        desc: 'Select option',
        agents: ['claude', 'codex', 'gemini'],
        match: (tail) => {
            const lines = tail.split('\n');
            // Find the LAST footer in tail. Claude Code renders its TodoWrite
            // status panel BELOW the prompt footer when tasks are active, so
            // the footer is often pushed 10-20 lines up from the bottom.
            // Bound depth from bottom (~30 lines) to skip stale footers in
            // scrollback. Mirrors routes/autoyes.py:_detect_autoyes_prompt.
            // "Press enter to confirm" is codex's footer — no separator glyph
            // and different wording, so the other three forms all miss it.
            const FOOTER_DEPTH_MAX = 30;
            const footerIdx = _findOptionFooter(lines);
            if (footerIdx >= 0 && (lines.length - 1 - footerIdx) <= FOOTER_DEPTH_MAX) {
                // The region floor is 60 lines, not 10. codex's "Yes, and don't
                // ask again for commands that start with `<command>`" embeds the
                // command and wraps 25-30 rows, pushing option 1 out of range so
                // only one option was counted and the bar never appeared.
                // Mirrors _OPTION_REGION_LOOKBACK in routes/autoyes.py.
                const sepIdx = _optionRegionStart(lines, footerIdx);
                return _optionCount(lines, sepIdx, footerIdx) >= 2;
            }
            return false;
        },
        // Detected (so the tab is marked and a notification fires) but with no
        // one-tap buttons — see _hasInternalDivider().
        notifyOnly: (tail) => {
            const lines = tail.split('\n');
            const footerIdx = _findOptionFooter(lines);
            if (footerIdx < 0) return false;
            return _hasInternalDivider(lines, _optionRegionStart(lines, footerIdx), footerIdx);
        },
        getActions: (tail) => {
            const actions = [];
            const lines = tail.split('\n');
            const footer = _findOptionFooter(lines);
            if (footer < 0) return null;
            const endIdx = footer;
            // Bound the prompt region above (same walk-up as match(), or the
            // two would disagree and getActions would return only the options
            // below an internal divider).
            const startIdx = _optionRegionStart(lines, endIdx);
            for (let i = startIdx; i < endIdx; i++) {
                const m = lines[i].match(_OPT_TEXT_RE);
                if (m) {
                    const num = m[1];
                    const text = m[2].trim();
                    if (text.length < 2) continue;
                    const label = text.length > 80 ? text.substring(0, 80) + '\u2026' : text;
                    actions.push({ label: num + '. ' + label, optNum: num, enter: false, color: 'cyan', isOption: true });
                }
            }
            if (actions.length < 2) return null;
            // Deduplicate by option number AND text
            const seenNum = new Set();
            const seenText = new Set();
            const deduped = actions.filter(a => {
                if (seenNum.has(a.optNum)) return false;
                const normText = a.label.replace(/^\d+\.\s*/, '').trim();
                if (seenText.has(normText)) return false;
                seenNum.add(a.optNum);
                seenText.add(normText);
                return true;
            });
            deduped.sort((a, b) => parseInt(a.optNum) - parseInt(b.optNum));
            return deduped.slice(0, 5);
        }
    },
];

function _unfreezeAndScroll() {
    _termPaused = false;
    _termHasNew = false;
    document.getElementById('term-new-output').classList.remove('visible');
    const display = document.getElementById('term-display');
    if (_termLatestContent) {
        const pre = document.getElementById('term-content');
        pre.innerHTML = ansiToHtml(_termLatestContent);
        _termLastContent = _termLatestContent;
    }
    display.scrollTop = display.scrollHeight;
}

// `content` must be ANSI-stripped; `target` keys the per-pane dismiss state.
function detectSmartActions(content, target, agentKind) {
    if (!content) return null;
    const st = _getSmartState(target);
    if (st.dismissedContent && content === st.dismissedContent) return null;
    // Reset dismiss if content changed
    if (st.dismissedContent && content !== st.dismissedContent) st.dismissedContent = null;

    const lines = content.split('\n');
    const tail = lines.slice(-60).join('\n');

    for (const pattern of SMART_PATTERNS) {
        if (pattern.agents && !pattern.agents.includes(agentKind)) continue;
        const match = pattern.match(tail);
        if (match) {
            const notifyOnly = !!(pattern.notifyOnly && pattern.notifyOnly(tail));
            // Static, not a predicate: whether a pattern represents something
            // WAITING on the human is a property of the pattern, not of the
            // pane's current text.
            const passive = !!pattern.passive;
            if (pattern.getActions) {
                const actions = pattern.getActions(tail, match);
                if (actions) return { id: pattern.id, desc: pattern.desc, actions, notifyOnly, passive };
            } else {
                return { id: pattern.id, desc: pattern.desc, actions: pattern.actions, notifyOnly, passive };
            }
        }
    }
    return null;
}

function renderSmartActions(result, targetOverride) {
    // notify-only detections still mark the tab and fire a push, but render no
    // action bar. Cleared here rather than at the call sites so every caller
    // (poll scan, WS stream, tab switch) is covered by one rule.
    if (result && result.notifyOnly) result = null;
    // Dedupe key must distinguish actions by what they SEND — numbered-options
    // and claude-resume actions have no `send`, so fall back to optNum/claudeCmd/label.
    const key = result ? result.id + '|' + result.actions.map(a => a.secret ? '***' : (a.send ?? a.optNum ?? a.claudeCmd ?? a.label)).join(',') : '';
    const target = targetOverride || _termTarget;

    const st = _getSmartState(target);
    if (st.key === key && _smartActionTarget === target) return;
    st.key = key;
    _smartActionTarget = target;

    const container = document.getElementById('smart-actions');
    if (!result) {
        container.classList.remove('visible');
        return;
    }

    const label = document.getElementById('smart-actions-label');
    const grid = document.getElementById('smart-actions-grid');

    // Show label with Auto-Yes toggle when applicable
    if (_isAutoYesCandidate(result)) {
        const session = _smartActionTarget ? _smartActionTarget.split(':')[0] : '';
        const active = isAutoYes(session);
        label.innerHTML = '';
        label.appendChild(document.createTextNode(result.desc));
        const toggle = document.createElement('button');
        toggle.className = 'sa-autoyes' + (active ? ' active' : '');
        toggle.textContent = active ? '\u26A1 Auto' : '\u26A1';
        toggle.addEventListener('click', toggleAutoYes);
        label.appendChild(toggle);
    } else {
        label.textContent = result.desc;
    }

    grid.innerHTML = '';
    grid.classList.toggle('sa-vertical', result.id === 'numbered-options');

    for (const action of result.actions) {
        const btn = document.createElement('button');
        btn.className = 'sa-btn sa-' + action.color;
        btn.textContent = action.label;
        if (action.noop) {
            btn.style.opacity = '0.5';
            btn.style.cursor = 'default';
        } else if (action.restart) {
            const cmd = action.claudeCmd;
            btn.addEventListener('click', () => restartClaudeSession(cmd));
        } else if (action.sudo) {
            // The matcher is the liveness proof for this exact pane. Do not run
            // the bottom-bar triple-tap flow, which derives its target from the
            // main input router and can send a secret to a different pane.
            const sudoTarget = target;
            btn.addEventListener('click', async () => {
                const sent = await _sendSudoPasswordToTerminal(sudoTarget);
                if (sent) hideSmartActions();
            });
        } else if (action.isOption) {
            const num = action.optNum;
            btn.addEventListener('click', () => sendSmartAction(num, true));
        } else if (action.keys) {
            const keys = action.keys;
            const kEnter = action.enter;
            btn.addEventListener('click', () => sendSmartKeyAction(keys, kEnter));
        } else {
            const send = action.send;
            const enter = action.enter;
            btn.addEventListener('click', () => sendSmartAction(send, enter));
        }
        grid.appendChild(btn);
    }

    // Guard: suppress scroll-freeze from the layout shift
    _layoutShifting = true;
    container.classList.add('visible');
    // Defer unfreeze to AFTER the layout reflow, so the scroll event
    // from the height change doesn't re-pause the terminal
    requestAnimationFrame(() => {
        requestAnimationFrame(() => {
            _unfreezeAndScroll();
            _layoutShifting = false;
        });
    });
}

function hideSmartActions() {
    _getSmartState(_smartActionTarget).key = '';
    _layoutShifting = true;
    document.getElementById('smart-actions').classList.remove('visible');
    requestAnimationFrame(() => {
        requestAnimationFrame(() => { _layoutShifting = false; });
    });
}

function dismissSmartActions() {
    const target = _smartActionTarget || _termTarget;
    // Use the content of whichever pane the actions came from (main or split)
    let content = _termLatestContent;
    if (target && target !== _termTarget) {
        const session = _termTarget ? _termTarget.split(':')[0] : '';
        const split = _splitPanes[session];
        if (split && split.target === target) content = split.lastContent;
    }
    // Store ANSI-STRIPPED content — detection always receives stripped
    // content, so a raw capture here would never match and the panel
    // would reappear on the next poll.
    _getSmartState(target).dismissedContent = content ? stripAnsi(content) : null;
    hideSmartActions();
}

// Send a sequence of named keys (e.g. arrow keys to move a TUI dialog's
// selection), then optionally Enter — for prompts that don't take typed text
// (OpenCode's permission dialog). Keys must be in the server's TMUX_KEY_MAP.
async function sendSmartKeyAction(keys, withEnter) {
    try {
        const target = _smartActionTarget || getInputTarget();
        for (const k of keys) {
            await fetch('/key', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ keys: k, target: target }),
            });
        }
        await sendSmartAction('', withEnter);
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

async function sendSmartAction(text, withEnter) {
    try {
        const target = _smartActionTarget || getInputTarget();
        const resp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ text: text, enter: !!withEnter, target: target }),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', data.via === 'tmux' ? 'Sent (tmux)' : 'Sent!');
            hideSmartActions();
            _unfreezeAndScroll();
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

async function restartClaudeSession(claudeCmd) {
    const target = _smartActionTarget || _termTarget;
    if (!target) { showFlash('error', 'No session'); return; }
    const session = target.split(':')[0];

    hideSmartActions();
    showFlash('sent', 'Restarting session\u2026');

    try {
        // 0. Grab CWD before killing (needed for renamed/duplicated tabs)
        let sessionCwd = '';
        try {
            const cwdResp = await fetch(`/terminal/cwd?session=${encodeURIComponent(session)}`);
            const cwdData = await cwdResp.json();
            if (cwdData.ok) sessionCwd = cwdData.cwd;
        } catch (_) {}

        // 1. Kill the old tmux session
        await fetch('/terminal/kill', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session: session }),
        });

        // Clean up command split pane
        onSessionKillCommands(session);

        // Brief pause for tmux cleanup
        await new Promise(r => setTimeout(r, 300));

        // 2. Re-launch via project launcher (venv only, skip init — we'll prompt)
        //    Pass cwd as fallback for renamed/duplicated tabs whose name != project dir
        const launchResp = await fetch('/terminal/launch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ project: session, cwd: sessionCwd, skip_init: true }),
        });
        const launchData = await launchResp.json();
        if (!launchData.ok) {
            showFlash('error', launchData.error || 'Relaunch failed');
            return;
        }

        // Update global target
        _termTarget = launchData.target;
        _smartActionTarget = launchData.target;
        updateTmuxIndicator();
        try { localStorage.setItem('term_target', _termTarget); } catch(e) {}

        // Set server target
        await fetch('/terminal/target', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ target: _termTarget }),
        });

        // Refresh session tabs
        await loadSessions();

        // 3. Prompt to run init command if configured
        let initWait = 500; // base wait for venv activation
        if (launchData.init_cmd && confirm('Run setup commands?\n\n' + launchData.init_cmd)) {
            await fetch('/terminal/run-init', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ session: session }),
            });
            initWait = 1500; // wait for init command to finish
        }
        await new Promise(r => setTimeout(r, initWait));

        // 4. Send the claude command
        const typeResp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ text: claudeCmd, enter: true, target: _termTarget }),
        });
        const typeData = await typeResp.json();
        if (typeData.ok) {
            showFlash('sent', 'Claude starting\u2026');
        } else {
            showFlash('error', typeData.error || 'Send failed');
        }

        // Resume terminal capture
        _termPaused = false;
        document.getElementById('term-display').classList.remove('hidden');
        startPolling();

    } catch (e) {
        showFlash('error', 'Restart failed: ' + e.message);
    }
}
