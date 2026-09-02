// input.js — Paste, copy, key, type, file upload, clipboard image, password-prompt detection, insert mode

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

    const attachBar = document.getElementById('attach-bar');
    if (attachBar) {
        attachBar.addEventListener('click', function(e) {
            const button = e.target.closest('[data-attach-remove]');
            if (button && attachBar.contains(button)) {
                removeAttachment(button.dataset.attachRemove);
            }
        });
    }
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
            <button class="attach-remove" data-attach-remove="${escHtml(a.id)}"
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
    // A password prompt is waiting, so what was typed is an answer to it, not a
    // prompt for an agent: send it byte-for-byte. trim() would eat a leading or
    // trailing space, and the server's conveniences (first-word case fix, [handle]
    // expansion, history) each rewrite or leak a password — `secret` turns them off.
    // Attachments mean this is an ordinary message, so it can never be a secret.
    // This legacy detector reads the main pane even when input routing points at
    // a split. In the F2 direction (main prompt, ordinary split) that only gives
    // user-typed text the stricter secret posture; unlike the vault key, this
    // path never selects and injects a stored value on the user's behalf.
    const hasAttachments = !!_attachments.length;
    let secret = !hasAttachments
        && typeof _isPasswordPrompt === 'function'
        && _isPasswordPrompt();
    const typed = input.value.replace(/\r/g, '').replace(/\n+$/, '');
    const raw = secret ? typed : typed.trim();
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
    if (hasAttachments) {
        const refs = _attachments.map(a => '@' + a.path).join(' ');
        finalText = raw ? raw + ' ' + refs : refs;
        clearAttachments();
    }

    // Step 2: resolve browser-only vault tokens at the last possible moment.
    // Resolution and the secret posture are one decision: once even one handle
    // resolves, no pane heuristic may put the value through expansion/history.
    // Attachments keep the whole message ordinary and every vault token literal.
    let expand = !secret;
    let vaultSentLiterally = false;
    let vaultLiteralNotice = 'Vault token sent literally';
    if (hasAttachments && typeof vaultScan === 'function') {
        const vaultResult = vaultScan(finalText);
        vaultSentLiterally = !!(vaultResult.used.length || vaultResult.missing.length);
    } else if (!hasAttachments && typeof vaultResolve === 'function') {
        const vaultResult = vaultResolve(finalText);
        if (vaultResult.used.length) {
            finalText = vaultResult.text;
            [secret, expand] = [true, false];
        }
        if (vaultResult.missing.length) {
            vaultSentLiterally = true;
            if (vaultResult.state === 'locked') {
                vaultLiteralNotice = 'Vault locked · token sent literally';
            } else if (vaultResult.state === 'unavailable') {
                vaultLiteralNotice = 'Vault unavailable · token sent literally';
            }
        }
    }

    // Step 3: send combined text
    try {
        const resp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            // expand:true is what turns [handle] into its segment body. Only the
            // composer opts in — the quick-action command buttons POST here too and
            // must keep sending shell text byte-for-byte.
            body: JSON.stringify({text: finalText, enter: true, expand: expand,
                                  secret: secret, target: getInputTarget()}),
        });
        const data = await resp.json();
        if (data.ok) {
            const grew = !secret && data.sent_chars && data.sent_chars > finalText.length;
            if (vaultSentLiterally) {
                showFlash('uploading', vaultLiteralNotice);
            } else {
                showFlash('sent', secret ? 'Password sent'
                                         : (grew ? 'Sent · ' + data.sent_chars + ' ch'
                                                 : (data.via === 'tmux' ? 'Sent (tmux)' : 'Sent!')));
            }
            lastAction = Date.now();
            updateStatusTime();
            loadHistory();
            // Sent: the draft is consumed. The Enter lock survives — it is a
            // property of the tab, not of the message (see clearDraftAfterSend).
            if (typeof clearDraftAfterSend === 'function') clearDraftAfterSend(draftTarget);
        } else {
            showFlash('error', data.error || 'Failed');
            input.value = raw;
            // Assigning .value does not fire the input event that owns the chip
            // strip, so restore the derived UI from the same token text too.
            if (typeof renderSegChips === 'function') renderSegChips();
            // Put it back so a failed send is not lost, but never persist a
            // password to the server-side draft store.
            if (!secret && typeof saveDraftSoon === 'function') saveDraftSoon();
        }
    } catch (e) {
        showFlash('error', 'Offline');
        input.value = raw;
        if (typeof renderSegChips === 'function') renderSegChips();
        if (!secret && typeof saveDraftSoon === 'function') saveDraftSoon();
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

// A single composer prefix — '@', '!', '/', '?'. Unlike typeCmd this must NOT
// press Enter: the whole value is the picker the CLI opens on the keystroke,
// and submitting a bare '@' would just send it as a prompt. no_history because
// one character is not a prompt worth recalling, and enter:false additionally
// keeps /type's fix_first_word_case() off it (routes/input.py only applies that
// when enter is true).
async function typePrefix(ch) {
    try {
        const resp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: ch, enter: false, no_history: true, target: getInputTarget()}),
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
// Password prompts — detected so manual typing and the optional browser-local
// vault both use the byte-exact secret path. Storage itself lives in vault.js.
// ================================================================
// Any pane waiting on a typed secret, not just sudo's. Deliberately WIDE: it
// only ever suppresses the composer's text conveniences, so a false positive
// costs one un-expanded, un-recorded send and nothing else. (It used to have a
// narrow sibling, _isSudoDetected(), gating a STORED sudo password — that
// password and everything that read it are gone; see routes/input.py.)
//
// OpenSSH is the case that matters and the one a sudo-shaped matcher misses: its
// prompt is "user@host's password: " — lowercase p, no "password for". So a password
// typed at an ssh prompt went through trim + fix_first_word_case + [handle]
// expansion + history like an ordinary message.
//
// Covered, all as the last non-empty line:
//   [sudo] password for user:
//   user@host's password:
//   Enter passphrase for key '/home/user/.ssh/id_ed25519':
//   Password:  /  Enter password:
// Not matched: "Permission denied (publickey,password)." — no trailing colon.
//
// Mirrors shared/tmux.py:PASSWORD_PROMPT_RE, which /type applies to the live pane
// when a caller does not set `secret`. This copy is the fast path — it also
// suppresses the trim and the draft write, which happen before any request — but
// it reads _termLatestContent, frozen while streaming is paused, so the server
// re-checks rather than trusting a missing flag.
const _PASSWORD_PROMPT_RE = /(?:^|[\s'"])(?:password|passphrase)(?:\s+for\b[^:]*)?:\s*$/i;

function _isPasswordPrompt() {
    if (!_termLatestContent) return false;
    const tail = stripAnsi(_termLatestContent).split('\n').slice(-20).join('\n');
    return _PASSWORD_PROMPT_RE.test(_lastNonEmptyLine(tail));
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
