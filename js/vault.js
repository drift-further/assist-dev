// vault.js — browser-local secret handles resolved immediately before /type.
//
// Values are plaintext in localStorage. The adapter keeps presence metadata
// separate from value reads so chip and settings renderers do not load secrets.
// Nothing outside this file reaches localStorage for vault data.

const VAULT_STORAGE_KEY = 'assist.vault.v1';
const VAULT_HANDLE_RE = /^[a-z0-9][a-z0-9._-]{1,31}$/;

// Deliberately no lookbehind. Older mobile Safari must be able to parse this
// file, so vaultResolve() checks the preceding backslash by hand just as the
// ordinary segment grammar does.
const VAULT_TOKEN_RE = /\[\$([a-z0-9][a-z0-9._-]{1,31})\]/g;

function _cleanVaultMap(loaded) {
    const clean = Object.create(null);
    if (!loaded || typeof loaded !== 'object' || Array.isArray(loaded)) return clean;
    for (const handle of Object.keys(loaded)) {
        if (VAULT_HANDLE_RE.test(handle) && typeof loaded[handle] === 'string') {
            clean[handle] = loaded[handle];
        }
    }
    return clean;
}

// Hydrate once instead of reparsing the plaintext map on every textarea input.
// A storage event invalidates this cache below, so another Assist tab still
// becomes authoritative the next time this tab reads vault state.
let _localStorageVaultCache = Object.create(null);
let _localStorageVaultState = 'cold';

function _hydrateLocalStorageVault() {
    if (_localStorageVaultState !== 'cold') return;
    const probe = VAULT_STORAGE_KEY + '.probe';
    try {
        localStorage.setItem(probe, '1');
        localStorage.removeItem(probe);
        const raw = localStorage.getItem(VAULT_STORAGE_KEY);
        _localStorageVaultCache = _cleanVaultMap(raw ? JSON.parse(raw) : {});
        _localStorageVaultState = 'ready';
    } catch (e) {
        _localStorageVaultCache = Object.create(null);
        _localStorageVaultState = 'unavailable';
    }
}

const localStorageVaultAdapter = {
    load: function() {
        _hydrateLocalStorageVault();
        if (_localStorageVaultState !== 'ready') return null;
        return Object.assign(Object.create(null), _localStorageVaultCache);
    },
    save: function(map) {
        const clean = _cleanVaultMap(map);
        try {
            localStorage.setItem(VAULT_STORAGE_KEY, JSON.stringify(clean));
            _localStorageVaultCache = clean;
            _localStorageVaultState = 'ready';
        } catch (e) {
            _localStorageVaultCache = Object.create(null);
            _localStorageVaultState = 'unavailable';
            throw e;
        }
    },
    available: function() {
        _hydrateLocalStorageVault();
        return _localStorageVaultState === 'ready';
    },
    has: function(handle) {
        _hydrateLocalStorageVault();
        return _localStorageVaultState === 'ready'
            && Object.prototype.hasOwnProperty.call(_localStorageVaultCache, handle);
    },
    handles: function() {
        _hydrateLocalStorageVault();
        return _localStorageVaultState === 'ready'
            ? Object.keys(_localStorageVaultCache).sort() : [];
    },
    state: function() {
        _hydrateLocalStorageVault();
        return _localStorageVaultState === 'ready' ? 'ready' : 'unavailable';
    },
    invalidate: function() {
        _localStorageVaultCache = Object.create(null);
        _localStorageVaultState = 'cold';
    },
};

let _vaultAdapter = localStorageVaultAdapter;

function vaultSetAdapter(adapter) {
    if (!adapter || typeof adapter.load !== 'function'
            || typeof adapter.save !== 'function'
            || typeof adapter.available !== 'function'
            || typeof adapter.has !== 'function'
            || typeof adapter.handles !== 'function'
            || typeof adapter.state !== 'function') {
        throw new TypeError('Vault adapter needs load(), save(), available(), has(), handles(), and state()');
    }
    _vaultAdapter = adapter;
}

function vaultState() {
    try {
        const state = _vaultAdapter.state();
        return state === 'ready' || state === 'locked' ? state : 'unavailable';
    } catch (e) {
        return 'unavailable';
    }
}

function _vaultMap(knownState) {
    try {
        if ((knownState || vaultState()) !== 'ready') return null;
        return _cleanVaultMap(_vaultAdapter.load());
    } catch (e) {
        return null;
    }
}

function vaultHas(handle) {
    if (vaultState() !== 'ready') return false;
    try {
        return !!_vaultAdapter.has(handle);
    } catch (e) {
        return false;
    }
}

function vaultGet(handle) {
    const map = _vaultMap();
    if (!map || !Object.prototype.hasOwnProperty.call(map, handle)) return undefined;
    return map[handle];
}

function vaultPut(handle, value) {
    handle = (handle || '').trim().toLowerCase();
    if (!VAULT_HANDLE_RE.test(handle) || typeof value !== 'string' || !value) return false;
    const map = _vaultMap();
    if (!map) return false;
    map[handle] = value;
    try {
        _vaultAdapter.save(map);
        return true;
    } catch (e) {
        return false;
    }
}

function vaultForget(handle) {
    const map = _vaultMap();
    if (!map) return false;
    delete map[handle];
    try {
        _vaultAdapter.save(map);
        return true;
    } catch (e) {
        return false;
    }
}

function vaultForgetAll() {
    try {
        if (vaultState() !== 'ready') return false;
        _vaultAdapter.save({});
        return true;
    } catch (e) {
        return false;
    }
}

function vaultHandles() {
    if (vaultState() !== 'ready') return [];
    try {
        return _vaultAdapter.handles()
            .filter(handle => VAULT_HANDLE_RE.test(handle)).sort();
    } catch (e) {
        return [];
    }
}

// Report token presence without constructing a string that contains any stored
// value. Composer guards and the per-keystroke chip renderer need only this
// classification; substitution belongs exclusively to the final send path.
function vaultScan(text) {
    const source = text || '';
    let state = vaultState();
    const used = [];
    const missing = [];
    VAULT_TOKEN_RE.lastIndex = 0;
    let match;
    while ((match = VAULT_TOKEN_RE.exec(source)) !== null) {
        if (match.index > 0 && source[match.index - 1] === '\\') continue;
        let present = false;
        if (state === 'ready') {
            try {
                present = !!_vaultAdapter.has(match[1]);
            } catch (e) {
                state = 'unavailable';
            }
        }
        const handles = present ? used : missing;
        if (!handles.includes(match[1])) handles.push(match[1]);
    }
    return { used: used, missing: missing, state: state };
}

function vaultResolve(text) {
    const source = text || '';
    const state = vaultState();
    const map = _vaultMap(state);
    const used = [];
    const missing = [];
    VAULT_TOKEN_RE.lastIndex = 0;
    const resolved = source.replace(VAULT_TOKEN_RE, function(token, handle, offset) {
        if (offset > 0 && source[offset - 1] === '\\') return token;
        if (!map || !Object.prototype.hasOwnProperty.call(map, handle)) {
            if (!missing.includes(handle)) missing.push(handle);
            return token;
        }
        if (!used.includes(handle)) used.push(handle);
        return map[handle];
    });
    return { text: resolved, used: used, missing: missing, state: state };
}

let _vaultQuickSentPrompt = null;

// The general password detector is intentionally broad because it only changes
// composer posture. Offering a stored value needs a much stronger claim: the
// last line must name sudo's own prompt shape. SSH and key-passphrase prompts
// therefore match no vault handle even when $sudo is the default entry.
const VAULT_SUDO_PROMPT_RE = /^\[sudo\]\s+password for\b[^:\n]*:\s*$/i;

function vaultPromptHandle(content) {
    if (!content || typeof stripAnsi !== 'function'
            || typeof _lastNonEmptyLine !== 'function') return null;
    const tail = stripAnsi(content).split('\n').slice(-20).join('\n');
    const line = _lastNonEmptyLine(tail);
    return VAULT_SUDO_PROMPT_RE.test(line) && vaultHas('sudo') ? 'sudo' : null;
}

function _vaultPromptKey() {
    return (_termTarget || '') + '\n' + (_termLatestContent || '');
}

function renderVaultQuickSend() {
    const btn = document.getElementById('vault-quick-send');
    if (!btn) return;
    const handle = vaultPromptHandle(_termLatestContent);
    const promptKey = _vaultPromptKey();
    if (_vaultQuickSentPrompt && _vaultQuickSentPrompt !== promptKey) {
        _vaultQuickSentPrompt = null;
    }
    // Gate and destination are the same pane. A main-pane prompt must not offer
    // its value while the composer is explicitly routed to the split pane.
    const sameTarget = typeof getInputTarget === 'function'
        && getInputTarget() === _termTarget;
    const waiting = !_vaultQuickSentPrompt && !!_termTarget && !!handle && sameTarget;
    btn.classList.toggle('hidden', !waiting);
    if (waiting) {
        btn.textContent = '$' + handle;
        btn.title = 'Send $' + handle + ' as a secret';
        btn.setAttribute('aria-label', btn.title);
    }
}

async function vaultSendDefault() {
    const handle = vaultPromptHandle(_termLatestContent);
    const value = handle ? vaultGet(handle) : undefined;
    // Recheck both the prompt binding and the exact target at tap time. Routing
    // can change after a render, and /type trusts this caller's secret posture.
    if (!handle || value === undefined || !_termTarget
            || getInputTarget() !== _termTarget) {
        renderVaultQuickSend();
        return;
    }
    try {
        const resp = await fetch('/type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: value, enter: true, expand: false,
                                  secret: true, target: _termTarget}),
        });
        const data = await resp.json();
        if (data.ok) {
            _vaultQuickSentPrompt = _vaultPromptKey();
            renderVaultQuickSend();
            showFlash('sent', 'Password sent');
            lastAction = Date.now();
            updateStatusTime();
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('storage', function(event) {
        if (_vaultAdapter !== localStorageVaultAdapter || event.key !== VAULT_STORAGE_KEY) return;
        localStorageVaultAdapter.invalidate();
        if (typeof _vaultUiChanged === 'function') _vaultUiChanged();
    });
}
