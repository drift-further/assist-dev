// actions.js — Smart action detection + rendering (SMART_PATTERNS)

// Auto-Yes: now server-side. UI just toggles and shows countdown.
let _autoyesState = {};  // session -> bool (mirrors server state)
let _autoyesCountdown = null;  // { target, remaining, delay, prompt_type } or null
let _autoyesDelay = (SETTINGS && SETTINGS.autoyes) ? SETTINGS.autoyes.default_delay : 5;  // current delay setting (persists across toggles)
let _autoyesPickerVisible = false;

function isAutoYes(session) {
    return !!_autoyesState[session];
}

// Show inline delay picker instead of browser prompt()
function showAutoYesPicker() {
    const target = _smartActionTarget || _termTarget;
    const session = target ? target.split(':')[0] : '';
    if (!session) return;

    // If already on, just turn off
    if (isAutoYes(session)) {
        _enableAutoYes(session, null);
        return;
    }

    // Show the picker
    const picker = document.getElementById('autoyes-picker');
    if (!picker) return;
    const valEl = document.getElementById('ay-pick-val');
    if (valEl) valEl.textContent = _autoyesDelay;
    picker.classList.add('visible');
    _autoyesPickerVisible = true;
}

function ayPickAdjust(delta) {
    _autoyesDelay = Math.max(1, Math.min(30, _autoyesDelay + delta));
    const valEl = document.getElementById('ay-pick-val');
    if (valEl) valEl.textContent = _autoyesDelay;
}

function ayPickConfirm() {
    const target = _smartActionTarget || _termTarget;
    const session = target ? target.split(':')[0] : '';
    document.getElementById('autoyes-picker').classList.remove('visible');
    _autoyesPickerVisible = false;
    if (session) _enableAutoYes(session, _autoyesDelay);
}

function ayPickCancel() {
    document.getElementById('autoyes-picker').classList.remove('visible');
    _autoyesPickerVisible = false;
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
            showFlash('sent', data.enabled ? `Auto-Yes ON (${delay}s)` : 'Auto-Yes OFF');
            updateAutoYesUI(session);
            _smartActionsKey = '';
            if (_termLatestContent) {
                const detected = detectSmartActions(stripAnsi(_termLatestContent));
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
}

// Sync auto-yes state from server on load / tab switch
async function syncAutoYesState() {
    try {
        const resp = await fetch('/autoyes/status');
        const data = await resp.json();
        _autoyesState = data.sessions || {};
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

let _countdownTimer = null;
function _renderAutoYesCountdown() {
    const bar = document.getElementById('autoyes-countdown');
    if (!bar) return;

    if (_countdownTimer) { clearInterval(_countdownTimer); _countdownTimer = null; }

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

// Check if a detected result qualifies for auto-yes (first option starts with "Yes")
function _isAutoYesCandidate(result) {
    if (!result) return false;
    if (result.id === 'permission-yna' || result.id === 'confirm-yn') return true;
    if (result.id !== 'numbered-options') return false;
    const first = result.actions[0];
    return first && first.isOption && /^1\.\s*Yes/i.test(first.label);
}

const SMART_PATTERNS = [
    {
        id: 'claude-resume',
        desc: 'Resume Claude session',
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
        id: 'confirm-yn',
        desc: 'Confirmation',
        match: (tail) => {
            // Only check last 8 lines — avoids false positives from answered prompts in scrollback
            const bottom = tail.split('\n').slice(-8).join('\n');
            if (/\(y\/n\/a\)/i.test(bottom) || /\[Y\/n\/a\]/i.test(bottom)) return false;
            return /\(y\/n\)/i.test(bottom) ||
                   /\[Y\/n\]/i.test(bottom) ||
                   /\[y\/N\]/i.test(bottom) ||
                   /\(yes\/no\)/i.test(bottom);
        },
        actions: [
            { label: 'Yes (y)', send: 'y', enter: false, color: 'green' },
            { label: 'No (n)', send: 'n', enter: false, color: 'red' },
        ]
    },
    {
        id: 'numbered-options',
        desc: 'Select option',
        match: (tail) => {
            const lines = tail.split('\n');
            // Footer must be near the bottom (last 6 lines) — if it's higher, the prompt is resolved
            const bottom = lines.slice(-6);
            const hasFooter = bottom.some(l => /(?:Enter to select|Esc to cancel)\s*[·•]/.test(l));
            if (!hasFooter) {
                // Fallback: options near the bottom (simple numbered prompts without footer)
                const last = lines.slice(-8);
                const optPattern = /^\s*(?:[^\d\s]\s*)?(\d+)[\.\)]\s+\S/;
                let count = 0;
                let lastOptIdx = -1;
                for (let i = 0; i < last.length; i++) {
                    if (optPattern.test(last[i])) { count++; lastOptIdx = i; }
                }
                return count >= 2 && lastOptIdx >= last.length - 3;
            }
            // Find region: between last ──── separator and footer
            let footerIdx = -1;
            for (let i = lines.length - 1; i >= 0; i--) {
                if (/(?:Enter to select|Esc to cancel)\s*[·•]/.test(lines[i])) { footerIdx = i; break; }
            }
            let sepIdx = 0;
            for (let i = footerIdx - 1; i >= 0; i--) {
                if (/^[\s]*─{10,}/.test(lines[i])) { sepIdx = i + 1; break; }
            }
            const optPattern = /^\s*(?:[^\d\s]\s*)?(\d+)[\.\)]\s+\S/;
            let count = 0;
            for (let i = sepIdx; i < footerIdx; i++) {
                if (optPattern.test(lines[i])) count++;
            }
            return count >= 2;
        },
        getActions: (tail) => {
            const actions = [];
            const lines = tail.split('\n');
            // Find footer line
            let endIdx = lines.length;
            for (let i = lines.length - 1; i >= 0; i--) {
                if (/(?:Enter to select|Esc to cancel)\s*[·•]/.test(lines[i])) { endIdx = i; break; }
            }
            // Find last ──── separator before footer (bounds the prompt region)
            let startIdx = 0;
            for (let i = endIdx - 1; i >= 0; i--) {
                if (/^[\s]*─{10,}/.test(lines[i])) { startIdx = i + 1; break; }
            }
            for (let i = startIdx; i < endIdx; i++) {
                const m = lines[i].match(/^\s*(?:[^\d\s]\s*)?(\d+)[\.\)]\s+(.+)/);
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

function detectSmartActions(content) {
    if (!content) return null;
    if (_smartDismissed && content === _smartDismissed) return null;
    // Reset dismiss if content changed
    if (_smartDismissed && content !== _smartDismissed) _smartDismissed = null;

    const lines = content.split('\n');
    const tail = lines.slice(-60).join('\n');

    for (const pattern of SMART_PATTERNS) {
        if (pattern.match(tail)) {
            if (pattern.getActions) {
                const actions = pattern.getActions(tail);
                if (actions) return { id: pattern.id, desc: pattern.desc, actions };
            } else {
                return { id: pattern.id, desc: pattern.desc, actions: pattern.actions };
            }
        }
    }
    return null;
}

function renderSmartActions(result, targetOverride) {
    const key = result ? result.id + '|' + result.actions.map(a => a.secret ? '***' : a.send).join(',') : '';
    const target = targetOverride || _termTarget;

    if (_smartActionsKey === key) return;
    _smartActionsKey = key;
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
        } else if (action.isOption) {
            const num = action.optNum;
            btn.addEventListener('click', () => sendSmartAction(num, true));
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
    _smartActionsKey = '';
    _layoutShifting = true;
    document.getElementById('smart-actions').classList.remove('visible');
    requestAnimationFrame(() => {
        requestAnimationFrame(() => { _layoutShifting = false; });
    });
}

function dismissSmartActions() {
    _smartDismissed = _termLatestContent;
    hideSmartActions();
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
