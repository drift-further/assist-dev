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

async function handleClipboardImage(blob) {
    // Generate a filename from type (e.g. image/png → clipboard_1711612800.png)
    const ext = blob.type.split('/')[1] || 'png';
    const ts = Math.floor(Date.now() / 1000);
    const file = new File([blob], 'clipboard_' + ts + '.' + ext, { type: blob.type });

    // Show uploading state in attach bar
    const attachBar = document.getElementById('attach-bar');
    const attachName = document.getElementById('attach-name');
    const attachSize = document.getElementById('attach-size');
    attachName.textContent = file.name;
    attachSize.textContent = formatFileSize(file.size);
    attachBar.classList.add('visible', 'uploading');

    // Show flash
    showFlash('uploading', 'Uploading image...');

    try {
        const fd = new FormData();
        fd.append('file', file);
        const resp = await fetch('/upload', { method: 'POST', body: fd });
        const data = await resp.json();
        if (data.ok) {
            // Store as attached file path (send flow will append it)
            _attachedFile = file;
            _attachedFilePath = data.path;
            attachName.textContent = file.name + ' \u2713';
            attachBar.classList.remove('uploading');
            showFlash('sent', 'Image ready');
        } else {
            attachBar.classList.remove('visible', 'uploading');
            showFlash('error', data.error || 'Upload failed');
        }
    } catch (e) {
        attachBar.classList.remove('visible', 'uploading');
        showFlash('error', 'Upload failed');
    }
}

async function doPaste() {
    if (_sending) return;
    const raw = input.value.replace(/\r/g, '').replace(/\n+$/, '').trim();
    if (!raw && !_attachedFile) {
        // Empty send = press Enter in terminal
        if (_termTarget) {
            try { await fetch('/type', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: '', enter: true, target: getInputTarget()}) }); } catch(e) {}
        }
        return;
    }
    _sending = true;
    input.value = '';
    let finalText = raw;

    // Step 1: upload file if attached
    if (_attachedFile) {
        // If already uploaded (clipboard image), use stored path
        if (_attachedFilePath) {
            const atRef = '@' + _attachedFilePath;
            finalText = raw ? raw + ' ' + atRef : atRef;
            removeAttachment();
        } else {
            showFlash('uploading', 'Uploading...');
            try {
                const fd = new FormData();
                fd.append('file', _attachedFile);
                const resp = await fetch('/upload', { method: 'POST', body: fd });
                const data = await resp.json();
                if (data.ok) {
                    const atRef = '@' + data.path;
                    finalText = raw ? raw + ' ' + atRef : atRef;
                    removeAttachment();
                } else {
                    showFlash('error', data.error || 'Upload failed');
                    input.value = raw;
                    _sending = false;
                    return;
                }
            } catch (e) {
                showFlash('error', 'Upload failed');
                input.value = raw;
                _sending = false;
                return;
            }
        }
    }

    // Step 2: send combined text
    try {
        const resp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: finalText, enter: true, target: getInputTarget()}),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', data.via === 'tmux' ? 'Sent (tmux)' : 'Sent!');
            lastAction = Date.now();
            updateStatusTime();
            loadHistory();
        } else {
            showFlash('error', data.error || 'Failed');
            input.value = raw;
        }
    } catch (e) {
        showFlash('error', 'Offline');
        input.value = raw;
    } finally {
        _sending = false;
    }
}

async function doCopy() {
    const text = input.value.trim();
    if (!text) return;
    try {
        const resp = await fetch('/copy', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: text}),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('copied', 'Copied!');
            lastAction = Date.now();
            updateStatusTime();
            loadHistory();
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

function triggerUpload() { document.getElementById('file-input').click(); }

function onFileSelected(inp) {
    const file = inp.files && inp.files[0];
    if (!file) return;
    if (file.size > 50 * 1024 * 1024) { showFlash('error', 'Too large (50MB max)'); inp.value = ''; return; }
    _attachedFile = file;
    document.getElementById('attach-name').textContent = file.name;
    document.getElementById('attach-size').textContent = formatFileSize(file.size);
    document.getElementById('attach-bar').classList.add('visible');
    inp.value = '';
}

function removeAttachment() {
    _attachedFile = null;
    _attachedFilePath = null;
    document.getElementById('attach-bar').classList.remove('visible', 'uploading');
}

async function toggleFavorite(text, event) {
    if (event) event.stopPropagation();
    try {
        const resp = await fetch('/favorite', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: text}),
        });
        await resp.json();
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

async function _sendSudoPasswordToTerminal() {
    // Server reads the stored password and types it into the pane \u2014
    // the password never travels to the browser or into history.
    try {
        const resp = await fetch('/sudo-send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ target: getInputTarget() }),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', 'Sudo sent');
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
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
