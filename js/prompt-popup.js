// prompt-popup.js — popups that tell you a pane is waiting on you. They never
// take focus, move layout or switch tabs by themselves, so one can surface
// mid-sentence without breaking what you are typing.
//
//   sudo      "Sudo password requested" for a pane anywhere at sudo's password
//             prompt. The server decides (shared/tmux.py:sudo_prompt_waiting):
//             sudo's own prompt as the last line AND a childless sudo in the
//             foreground of that pane's tty. Send types this browser's vault
//             $sudo value as a secret and carries expect_prompt_pid, so the
//             server refuses (409 prompt_gone) once that sudo has stopped
//             waiting. With no $sudo stored, the button opens the tab instead.
//   question  "Question pending" for a prompt the action bar detects in a tab
//             you are not looking at, unless Auto-Yes is about to answer it.
//             It answers nothing; its one button brings up the tab.
//
// Settings → UI & Behavior: popup_autohide "on" hides each popup after
// popup_seconds; "off" keeps it until acted on, closed with ×, or the prompt
// goes away.

const PROMPT_POPUP_MAX = 3;

const _promptPopupHost = document.createElement('div');
_promptPopupHost.className = 'prompt-popup-host';
document.body.appendChild(_promptPopupHost);

// key -> {el, kind, timer}, for the popups on screen.
const _promptPopups = new Map();
// Keys already shown in this browser, per kind. A prompt pops once however long
// it waits; a new sudo (new pid) or a new prompt (new fingerprint) pops again.
const _promptPopupSeen = {sudo: new Set(), question: new Set()};
// key -> {kind, build}: live prompts waiting for a slot. Shared by both kinds and
// kept in arrival order, so a sudo already waiting is not overtaken by questions
// that arrive later.
const _promptPopupPending = new Map();

function _promptPopupAutoHide() {
    return !(SETTINGS && SETTINGS.ui && SETTINGS.ui.popup_autohide === 'off');
}

function _promptPopupMs() {
    const seconds = Number(SETTINGS && SETTINGS.ui && SETTINGS.ui.popup_seconds);
    return (seconds >= 1 ? Math.min(seconds, 60) : 5) * 1000;
}

// One kind's snapshot for this poll. Forget and close what went away, queue what
// is new, then fill free slots oldest first. A prompt counts as seen only once
// it is shown, or when its tab is in view: looking at the tab is seeing the
// prompt, so leaving the tab does not pop a notification for it.
function _syncPromptPopups(kind, entries) {
    const seen = _promptPopupSeen[kind];
    const live = new Set(entries.map(entry => entry.key));
    for (const key of seen) {
        if (!live.has(key)) seen.delete(key);
    }
    for (const [key, pending] of _promptPopupPending) {
        if (pending.kind === kind && !live.has(key)) _promptPopupPending.delete(key);
    }
    for (const [key, popup] of _promptPopups) {
        if (popup.kind === kind && !live.has(key)) _closePromptPopup(key);
    }
    for (const entry of entries) {
        if (entry.inView) {
            seen.add(entry.key);
            _promptPopupPending.delete(entry.key);
            if (_promptPopups.has(entry.key)) _closePromptPopup(entry.key);
        } else if (!seen.has(entry.key) && !_promptPopupPending.has(entry.key)) {
            _promptPopupPending.set(entry.key, {kind, build: entry.build});
        }
    }
    _promotePromptPopups();
}

function _promotePromptPopups() {
    for (const [key, pending] of _promptPopupPending) {
        if (_promptPopups.size >= PROMPT_POPUP_MAX) return;
        _promptPopupPending.delete(key);
        _promptPopupSeen[pending.kind].add(key);
        _showPromptPopup(pending.kind, key, pending.build());
    }
}

function _promptPopupWhere(target, session) {
    const pane = (_sessionPanes || []).find(p => p.target === target);
    const agent = pane ? agentDisplayName(pane) : null;
    return agent || shortName(session || target.split(':')[0]);
}

// spec: {title, detail, detailTitle, button: {label, color, run}}
function _showPromptPopup(kind, key, spec) {
    const el = document.createElement('div');
    el.className = 'prompt-popup ' + kind;
    el.setAttribute('role', 'status');

    const text = document.createElement('div');
    text.className = 'prompt-popup-text';
    const where = document.createElement('div');
    where.className = 'prompt-popup-where';
    where.textContent = spec.title;
    const detail = document.createElement('div');
    detail.className = 'prompt-popup-detail';
    detail.textContent = spec.detail;
    detail.title = spec.detailTitle || spec.detail;
    text.appendChild(where);
    text.appendChild(detail);
    el.appendChild(text);

    const actions = document.createElement('div');
    actions.className = 'prompt-popup-actions';
    actions.appendChild(_promptPopupButton(key, spec.button.label, 'pp-' + spec.button.color, spec.button.run));
    el.appendChild(actions);
    el.appendChild(_promptPopupButton(key, '×', 'prompt-popup-close', () => {}));

    const autoHide = _promptPopupAutoHide();
    if (autoHide) {
        const timer = document.createElement('div');
        timer.className = 'prompt-popup-timer';
        timer.style.animationDuration = _promptPopupMs() + 'ms';
        el.appendChild(timer);
    }

    // pointerdown preventDefault keeps the composer focused, and the phone
    // keyboard up, through a tap — the segment chips and autocomplete do the same.
    el.addEventListener('pointerdown', e => { e.preventDefault(); });

    _promptPopupHost.appendChild(el);
    const popup = {el, kind, timer: null};
    _promptPopups.set(key, popup);
    requestAnimationFrame(() => el.classList.add('visible'));
    if (autoHide) popup.timer = setTimeout(() => _closePromptPopup(key), _promptPopupMs());
}

function _promptPopupButton(key, label, className, action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className;
    btn.textContent = label;
    btn.title = label === '×' ? 'Close' : label;
    btn.tabIndex = -1;
    btn.addEventListener('click', e => {
        e.stopPropagation();
        _closePromptPopup(key);
        action();
    });
    return btn;
}

function _closePromptPopup(key) {
    const popup = _promptPopups.get(key);
    if (!popup) return;
    _promptPopups.delete(key);
    clearTimeout(popup.timer);
    popup.el.classList.remove('visible');
    setTimeout(() => popup.el.remove(), 200);
    _promotePromptPopups();
}

// ---------------------------------------------------------------- sudo

function _applySudoPrompts(prompts) {
    _syncPromptPopups('sudo', (prompts || [])
        .filter(p => p && p.target && p.pid)
        .map(p => ({key: p.target + '#' + p.pid, build: () => _sudoPopupSpec(p)})));
}

// sudo's own argv minus the word sudo: "sudo apt install tmux" -> "apt install tmux".
function _sudoPopupCommand(prompt) {
    return String(prompt.command || '').replace(/^\S*\bsudo\s*/, '') || 'sudo';
}

function _sudoPopupSpec(prompt) {
    // With no $sudo stored there is nothing to send, so the button opens the tab
    // to type it there. Never report a password sent that was not.
    const canSend = typeof vaultHas === 'function' && vaultHas('sudo');
    return {
        title: 'Sudo password requested · ' + _promptPopupWhere(prompt.target, prompt.session),
        detail: _sudoPopupCommand(prompt),
        detailTitle: String(prompt.command || ''),
        button: canSend
            ? {label: 'Send', color: 'green', run: () => _sudoPopupSend(prompt)}
            : {label: 'Open tab', color: 'cyan', run: () => selectTab(prompt.target)},
    };
}

async function _sudoPopupSend(prompt) {
    const value = typeof vaultGet === 'function' ? vaultGet('sudo') : undefined;
    if (value === undefined) {
        showFlash('error', 'No $sudo in this browser');
        return;
    }
    try {
        const resp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                text: value, enter: true, expand: false, secret: true,
                target: prompt.target, expect_prompt_pid: prompt.pid,
            }),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', 'Password sent');
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

// ---------------------------------------------------------------- questions

// entries: {target, session, detected, prompt: {fp, summary, autoyes}} for each
// tab not in view where the action bar's detection found something waiting, plus
// {target, prompt, inView: true} for the tab in view.
function _applyQuestionPrompts(entries) {
    _syncPromptPopups('question', (entries || [])
        .filter(q => q && q.target && q.prompt && q.prompt.fp
                     && (q.inView || (q.detected && !q.prompt.autoyes)))
        .map(q => ({key: q.target + '#' + q.prompt.fp, inView: !!q.inView,
                    build: () => _questionPopupSpec(q)})));
}

function _questionPopupSpec(q) {
    return {
        title: 'Question pending · ' + _promptPopupWhere(q.target, q.session),
        detail: q.prompt.summary || q.detected.desc,
        button: {label: 'Open tab', color: 'cyan', run: () => selectTab(q.target)},
    };
}
