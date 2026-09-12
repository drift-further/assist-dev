// sudo-prompt.js — a 5-second popup when a pane anywhere is waiting at sudo's
// password prompt, naming the command and offering Yes / No.
//
// The server decides what qualifies (shared/tmux.py:sudo_prompt_waiting): sudo's
// own prompt as the pane's last line AND a childless sudo process in the
// foreground of that pane's tty. This file only shows it. The popup must cost the
// composer nothing — it takes no focus, moves no layout and switches no tab — so
// a prompt can surface mid-sentence without breaking what you are typing.
//
// Yes types this browser's vault $sudo value as a secret; No sends Ctrl-C. Both
// name the sudo pid, and the server refuses (409 prompt_gone) once that process
// has stopped waiting, so a late tap cannot type a password into a shell.

const SUDO_POPUP_MS = 5000;
const SUDO_POPUP_MAX = 3;

const _sudoPopupHost = document.createElement('div');
_sudoPopupHost.className = 'sudo-popup-host';
document.body.appendChild(_sudoPopupHost);

// target#pid -> popup element, for the ones on screen.
const _sudoPopups = new Map();
// target#pid already shown in this browser. One sudo process pops once however
// long it waits; the next sudo has a new pid and pops again.
const _sudoPopupSeen = new Set();

function _applySudoPrompts(prompts) {
    const live = new Set();
    for (const prompt of prompts || []) {
        if (!prompt || !prompt.target || !prompt.pid) continue;
        const key = prompt.target + '#' + prompt.pid;
        live.add(key);
        if (!_sudoPopupSeen.has(key)) _showSudoPopup(prompt, key);
    }
    for (const key of _sudoPopupSeen) {
        if (!live.has(key)) _sudoPopupSeen.delete(key);
    }
    for (const key of _sudoPopups.keys()) {
        if (!live.has(key)) _closeSudoPopup(key);
    }
}

function _sudoPopupWhere(prompt) {
    const pane = (_sessionPanes || []).find(p => p.target === prompt.target);
    const agent = pane ? agentDisplayName(pane) : null;
    return agent || shortName(prompt.session || prompt.target.split(':')[0]);
}

// sudo's own argv minus the word sudo: "sudo apt install tmux" -> "apt install tmux".
function _sudoPopupCommand(prompt) {
    return String(prompt.command || '').replace(/^\S*\bsudo\s*/, '') || 'sudo';
}

function _showSudoPopup(prompt, key) {
    _sudoPopupSeen.add(key);
    const canSend = typeof vaultHas === 'function' && vaultHas('sudo');

    const el = document.createElement('div');
    el.className = 'sudo-popup';
    el.setAttribute('role', 'status');

    const text = document.createElement('div');
    text.className = 'sudo-popup-text';
    const where = document.createElement('div');
    where.className = 'sudo-popup-where';
    where.textContent = 'sudo · ' + _sudoPopupWhere(prompt);
    const cmd = document.createElement('div');
    cmd.className = 'sudo-popup-cmd';
    cmd.textContent = _sudoPopupCommand(prompt);
    cmd.title = String(prompt.command || '');
    text.appendChild(where);
    text.appendChild(cmd);
    el.appendChild(text);

    // With no $sudo stored there is nothing to send, so Yes becomes Open: go to
    // the pane and type it there. Never report a password sent that was not.
    el.appendChild(_sudoPopupButton(key, canSend ? 'Yes' : 'Open', 'sudo-popup-yes',
        () => canSend ? _sudoPopupYes(prompt) : selectTab(prompt.target)));
    el.appendChild(_sudoPopupButton(key, 'No', 'sudo-popup-no', () => _sudoPopupNo(prompt)));

    const timer = document.createElement('div');
    timer.className = 'sudo-popup-timer';
    timer.style.animationDuration = SUDO_POPUP_MS + 'ms';
    el.appendChild(timer);

    // pointerdown preventDefault keeps the composer focused, and the phone
    // keyboard up, through a tap — the segment chips and autocomplete do the same.
    el.addEventListener('pointerdown', e => { e.preventDefault(); });

    _sudoPopupHost.appendChild(el);
    _sudoPopups.set(key, el);
    requestAnimationFrame(() => el.classList.add('visible'));
    el._sudoTimer = setTimeout(() => _closeSudoPopup(key), SUDO_POPUP_MS);

    while (_sudoPopups.size > SUDO_POPUP_MAX) {
        _closeSudoPopup(_sudoPopups.keys().next().value);
    }
}

function _sudoPopupButton(key, label, className, action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className;
    btn.textContent = label;
    btn.tabIndex = -1;
    btn.addEventListener('click', e => {
        e.stopPropagation();
        _closeSudoPopup(key);
        action();
    });
    return btn;
}

function _closeSudoPopup(key) {
    const el = _sudoPopups.get(key);
    if (!el) return;
    _sudoPopups.delete(key);
    clearTimeout(el._sudoTimer);
    el.classList.remove('visible');
    setTimeout(() => el.remove(), 200);
}

async function _sudoPopupYes(prompt) {
    const value = typeof vaultGet === 'function' ? vaultGet('sudo') : undefined;
    if (value === undefined) {
        showFlash('error', 'No $sudo in this browser');
        return;
    }
    await _sudoPopupPost('/type', {
        text: value, enter: true, expand: false, secret: true,
        target: prompt.target, expect_prompt_pid: prompt.pid,
    }, 'Password sent');
}

async function _sudoPopupNo(prompt) {
    await _sudoPopupPost('/key', {
        keys: 'ctrl+c', target: prompt.target, expect_prompt_pid: prompt.pid,
    }, 'sudo cancelled');
}

async function _sudoPopupPost(url, body, doneText) {
    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', doneText);
            lastAction = Date.now();
            updateStatusTime();
        } else {
            showFlash('error', data.error === 'prompt_gone'
                ? 'sudo is no longer waiting' : (data.error || 'Failed'));
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}
