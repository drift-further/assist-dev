// input.js — Paste, copy, key, type, file upload, clipboard image, sudo password, insert mode

// ================================================================
// Clipboard Image Paste — intercept paste events with image data
// ================================================================
function initClipboardImagePaste() {
    const textarea = document.getElementById('text-input');
    textarea.addEventListener('paste', function(e) {
        const items = (e.clipboardData || {}).items;
        if (!items) return;
        for (let i = 0; i < items.length; i++) {
            if (items[i].type.indexOf('image/') === 0) {
                e.preventDefault();
                const blob = items[i].getAsFile();
                if (blob) handleClipboardImage(blob);
                return;
            }
        }
        // No image — let normal text paste through
    });
}

function handleClipboardImage(blob) {
    // Generate a filename from type (e.g. image/png -> clipboard_1711612800.png)
    const ext = blob.type.split('/')[1] || 'png';
    const ts = Math.floor(Date.now() / 1000);
    addAttachment(new File([blob], 'clipboard_' + ts + '.' + ext, { type: blob.type }));
}

// ================================================================
// Attachments — several files per message
// ================================================================
// Each file uploads the moment it is attached rather than on send, so the
// transfer overlaps with typing and doPaste() only has to append paths.
// /upload takes one file per request, so N files is N requests and the
// server's streaming and size checks are reused untouched.

let _attachSeq = 0;

function addAttachment(file) {
    const maxMb = (SETTINGS && SETTINGS.limits && SETTINGS.limits.max_upload_mb) || 2048;
    if (file.size > maxMb * 1024 * 1024) {
        showFlash('error', file.name + ' too large (' + maxMb + 'MB max)');
        return;
    }
    const entry = {
        id: 'a' + (++_attachSeq),
        name: file.name,
        size: file.size,
        path: null,
        uploading: true,
    };
    _attachments.push(entry);
    renderAttachments();
    // The tray travels with the tab's draft, so every mutation is a draft edit.
    if (typeof saveDraftSoon === 'function') saveDraftSoon();
    _uploadAttachment(entry, file);
}

async function _uploadAttachment(entry, file) {
    try {
        const fd = new FormData();
        fd.append('file', file);
        const resp = await fetch('/upload', { method: 'POST', body: fd });
        const data = await resp.json();
        if (data.ok) {
            entry.path = data.path;
            entry.uploading = false;
        } else {
            _attachments = _attachments.filter(a => a.id !== entry.id);
            showFlash('error', data.error || (file.name + ': upload failed'));
        }
    } catch (e) {
        _attachments = _attachments.filter(a => a.id !== entry.id);
        showFlash('error', file.name + ': upload failed');
    }
    renderAttachments();
    // Only now does the entry carry a path, which is the part the draft can
    // actually store — save again so the tray survives a tab switch.
    if (typeof saveDraftSoon === 'function') saveDraftSoon();
}

function renderAttachments() {
    const bar = document.getElementById('attach-bar');
    if (!bar) return;
    if (!_attachments.length) {
        bar.classList.remove('visible');
        bar.innerHTML = '';
        return;
    }
    bar.innerHTML = _attachments.map(a => `
        <span class="attach-chip${a.uploading ? ' uploading' : ''}">
            <span class="attach-chip-name">&#128206; ${escHtml(a.name)}</span>
            <span class="attach-chip-size">${a.uploading ? '…' : formatFileSize(a.size)}</span>
            <button class="attach-remove" onclick="removeAttachment('${a.id}')"
                    aria-label="Remove ${escHtml(a.name)}">&times;</button>
        </span>`).join('');
    bar.classList.add('visible');
}

async function doPaste() {
    if (_sending) return;
    // The tab whose draft this message IS. Captured up front: a tab switch
    // mid-send must not clear the wrong tab's draft. Note this is _termTarget,
    // not getInputTarget() — input routing can aim the send at a split pane,
    // but the composer still belongs to the tab on screen.
    const draftTarget = (typeof _draftTarget === 'function') ? _draftTarget() : '';
    if (typeof draftCancelPendingSave === 'function') draftCancelPendingSave(draftTarget);
    const raw = input.value.replace(/\r/g, '').replace(/\n+$/, '').trim();
    if (_attachments.some(a => a.uploading)) {
        showFlash('uploading', 'Still uploading…');
        return;
    }
    if (!raw && !_attachments.length) {
        // Empty send = press Enter in terminal
        if (_termTarget) {
            try { await fetch('/type', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: '', enter: true, target: getInputTarget()}) }); } catch(e) {}
        }
        return;
    }
    _sending = true;
    input.value = '';
    if (typeof renderSegChips === 'function') renderSegChips();
    let finalText = raw;

    // Step 1: append one @ref per attachment. They uploaded when they were
    // attached, and the guard above already refused to send while any are still
    // in flight, so every entry here has a path.
    if (_attachments.length) {
        const refs = _attachments.map(a => '@' + a.path).join(' ');
        finalText = raw ? raw + ' ' + refs : refs;
        clearAttachments();
    }

    // Step 2: send combined text
    try {
        const resp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            // expand:true is what turns [handle] into its segment body. Only the
            // composer opts in — the quick-action command buttons POST here too and
            // must keep sending shell text byte-for-byte.
            body: JSON.stringify({text: finalText, enter: true, expand: true, target: getInputTarget()}),
        });
        const data = await resp.json();
        if (data.ok) {
            const grew = data.sent_chars && data.sent_chars > finalText.length;
            showFlash('sent', grew ? 'Sent · ' + data.sent_chars + ' ch'
                                   : (data.via === 'tmux' ? 'Sent (tmux)' : 'Sent!'));
            lastAction = Date.now();
            updateStatusTime();
            loadHistory();
            // Sent: the draft is consumed. The Enter lock survives — it is a
            // property of the tab, not of the message (see clearDraftAfterSend).
            if (typeof clearDraftAfterSend === 'function') clearDraftAfterSend(draftTarget);
        } else {
            showFlash('error', data.error || 'Failed');
            input.value = raw;
            if (typeof saveDraftSoon === 'function') saveDraftSoon();
        }
    } catch (e) {
        showFlash('error', 'Offline');
        input.value = raw;
        if (typeof saveDraftSoon === 'function') saveDraftSoon();
    } finally {
        _sending = false;
    }
}

function triggerUpload() { document.getElementById('file-input').click(); }

function onFileSelected(inp) {
    // The picker is `multiple`, and tapping attach again adds to the tray rather
    // than replacing it — both ways of building up a set on a phone.
    for (const file of inp.files || []) addAttachment(file);
    inp.value = '';
}

function removeAttachment(id) {
    _attachments = _attachments.filter(a => a.id !== id);
    renderAttachments();
    if (typeof saveDraftSoon === 'function') saveDraftSoon();
}

function clearAttachments() {
    _attachments = [];
    renderAttachments();
}

async function toggleFavorite(text, event) {
    if (event) event.stopPropagation();
    try {
        const resp = await fetch('/favorite', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: text}),
        });
        const data = await resp.json();
        // The server refuses to unstar a named segment on a bare tap — other prompts
        // may reference it. Open the editor so the delete is a deliberate act.
        if (data.action === 'kept' && data.id) {
            segEdit(data.id);
            return;
        }
        loadHistory();
    } catch (e) {}
}

async function clearHistory() {
    try {
        await fetch('/history', {method: 'DELETE'});
        loadHistory();
    } catch (e) {}
}

async function loadHistory() {
    try {
        const resp = await fetch('/history');
        const data = await resp.json();
        _history = data.history || [];
        _favorites = data.favorites || [];
        renderLists();
    } catch (e) {}
}

async function sendKey(keys) {
    try {
        const resp = await fetch('/key', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({keys: keys, target: getInputTarget()}),
        });
        const data = await resp.json();
        if (data.ok) {
            lastAction = Date.now();
            updateStatusTime();
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

async function typeCmd(cmd) {
    try {
        const resp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: cmd, enter: true, target: getInputTarget()}),
        });
        const data = await resp.json();
        if (data.ok) {
            lastAction = Date.now();
            updateStatusTime();
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

// ================================================================
// Sudo Password — stored server-side only; the client never sees it
// ================================================================
let _sudoHasPassword = false;

async function initSudoButton() {
    const btn = document.getElementById('btn-sudo');
    try {
        const resp = await fetch('/sudo-password');
        const data = await resp.json();
        _sudoHasPassword = !!data.has_password;
        if (!_sudoHasPassword) {
            // Migrate a legacy localStorage password to the server, then drop it
            const local = localStorage.getItem('assist_sudo_pw');
            if (local) {
                _sudoHasPassword = true;
                fetch('/sudo-password', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ password: local }),
                }).catch(() => {});
            }
        }
    } catch (e) {
        _sudoHasPassword = false;
    }
    // Never keep the password client-side
    localStorage.removeItem('assist_sudo_pw');
    if (_sudoHasPassword) {
        btn.classList.add('has-pw');
        btn.innerHTML = '&#128275;'; // open lock
    }
}

async function toggleSudoPassword() {
    const btn = document.getElementById('btn-sudo');
    if (_sudoHasPassword) {
        if (confirm('Clear stored sudo password?')) {
            _sudoHasPassword = false;
            localStorage.removeItem('assist_sudo_pw');
            fetch('/sudo-password', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ clear: true }),
            }).catch(() => {});
            btn.classList.remove('has-pw');
            btn.innerHTML = '&#128274;'; // closed lock
            showFlash('sent', 'Password cleared');
        }
    } else {
        const pw = prompt('Enter sudo password (stored on server):');
        if (pw) {
            try {
                await fetch('/sudo-password', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ password: pw }),
                });
            } catch (e) {}
            _sudoHasPassword = true;
            btn.classList.add('has-pw');
            btn.innerHTML = '&#128275;'; // open lock
            showFlash('sent', 'Password stored');
        }
    }
}

// ================================================================
// Sudo Send Button — bottom bar, replaces COPY
// 1-tap when sudo prompt detected; 3-tap within 5s otherwise
// ================================================================
let _sudoTapCount = 0;
let _sudoTapTimer = null;
const _SUDO_TAP_WINDOW = 5000;
const _SUDO_TAP_REQUIRED = 3;

function _isSudoDetected() {
    if (!_termLatestContent) return false;
    const tail = stripAnsi(_termLatestContent).split('\n').slice(-20).join('\n');
    return /\[sudo\] password for/.test(tail) ||
           /Password:\s*$/.test(tail.trimEnd()) ||
           /password for .+:\s*$/.test(tail.trimEnd());
}

function _updateSudoSendBtn() {
    const btn = document.getElementById('btn-sudo-send');
    if (!btn) return;
    const detected = _sudoTapCount === 0 && _isSudoDetected();
    btn.classList.toggle('sudo-detected', detected);
    btn.classList.toggle('sudo-tapping-1', _sudoTapCount === 1);
    btn.classList.toggle('sudo-tapping-2', _sudoTapCount === 2);
}

function _resetSudoTap() {
    _sudoTapCount = 0;
    if (_sudoTapTimer) { clearTimeout(_sudoTapTimer); _sudoTapTimer = null; }
    _updateSudoSendBtn();
}

async function _sendSudoPasswordToTerminal(target = getInputTarget()) {
    // Server reads the stored password and types it into the pane \u2014
    // the password never travels to the browser or into history.
    try {
        const resp = await fetch('/sudo-send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ target }),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', 'Sudo sent');
            return true;
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
    return false;
}

async function doSudoSend() {
    if (!_sudoHasPassword) {
        showFlash('error', 'No password (use \uD83D\uDD12 to set)');
        return;
    }

    // Single-tap mode when sudo prompt is visible in terminal
    if (_isSudoDetected()) {
        await _sendSudoPasswordToTerminal();
        _resetSudoTap();
        return;
    }

    // Triple-tap mode: require 3 taps within 5 seconds
    _sudoTapCount++;
    if (_sudoTapTimer) clearTimeout(_sudoTapTimer);

    if (_sudoTapCount >= _SUDO_TAP_REQUIRED) {
        await _sendSudoPasswordToTerminal();
        _resetSudoTap();
        return;
    }

    _updateSudoSendBtn();
    _sudoTapTimer = setTimeout(() => _resetSudoTap(), _SUDO_TAP_WINDOW);
}

async function sendInsertMode() {
    // Send single 'i' without Enter to re-enter Claude Code insert mode
    try {
        const resp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ text: 'i', enter: false, target: getInputTarget() }),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', 'Insert mode');
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}
