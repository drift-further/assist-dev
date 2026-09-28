// js/keycombo.js — the "Any key" composer in the More Keys drawer.
//
// Ctrl/Alt/Shift are sticky toggles; one tap on a key (or one typed character)
// sends that combo to the active pane as POST /key {combo}, then clears them.
// The server owns the grammar (shared/key_combo.py) and refuses what tmux
// cannot name, so this file only collects the combo and shows what was sent.

const _comboMods = { ctrl: false, alt: false, shift: false };
const _comboNames = {
    Escape: 'Esc', BSpace: 'Backspace', Insert: 'Ins', Delete: 'Del',
    Left: '←', Up: '↑', Down: '↓', Right: '→',
};

function toggleKeyCombo() {
    const drawer = document.getElementById('drawer-left');
    const on = drawer.classList.toggle('combo-mode');
    const btn = document.getElementById('combo-toggle');
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.textContent = on ? 'All keys' : 'Any key';
    _clearComboMods();
    _setComboStatus('', false);
    drawer.scrollTop = 0;
}

function toggleComboMod(name) {
    _comboMods[name] = !_comboMods[name];
    _renderComboMods();
}

function _clearComboMods() {
    _comboMods.ctrl = _comboMods.alt = _comboMods.shift = false;
    _renderComboMods();
}

function _comboPrefix() {
    return (_comboMods.ctrl ? 'Ctrl+' : '') + (_comboMods.alt ? 'Alt+' : '')
        + (_comboMods.shift ? 'Shift+' : '');
}

function _renderComboMods() {
    document.querySelectorAll('.combo-mod').forEach(btn => {
        btn.setAttribute('aria-pressed', _comboMods[btn.dataset.mod] ? 'true' : 'false');
    });
    const prefix = _comboPrefix();
    if (prefix) _setComboStatus(prefix + '…', true);
}

function _setComboStatus(text, armed) {
    const el = document.getElementById('combo-status');
    el.textContent = text || 'Pick modifiers, then tap a key';
    el.classList.toggle('armed', !!armed);
}

async function sendCombo(key) {
    const combo = { ctrl: _comboMods.ctrl, alt: _comboMods.alt, shift: _comboMods.shift, key: key };
    const label = _comboPrefix() + (_comboNames[key] || key);
    _clearComboMods();
    try {
        const resp = await fetch('/key', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({combo: combo, target: getInputTarget()}),
        });
        const data = await resp.json();
        if (data.ok) {
            lastAction = Date.now();
            updateStatusTime();
            _setComboStatus('Sent ' + label, false);
        } else {
            _setComboStatus(label + ': ' + (data.error || 'failed'), false);
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        _setComboStatus('', false);
        showFlash('error', 'Offline');
    }
}

// One typed character is one combo. The field empties itself so the next
// character is a fresh send; the keyboard stays up for it.
document.getElementById('combo-char').addEventListener('input', e => {
    const value = e.target.value;
    e.target.value = '';
    if (value) sendCombo(value.slice(-1));
});

_setComboStatus('', false);
