// autocomplete.js — Inline @file and /skill typeahead for the composer.
// Detects an @path or /skill token at the caret, fetches candidates from the
// /complete/* endpoints, and inserts the chosen string into the textarea.
// Nothing is sent here — composed text still leaves through doPaste().

const _acPopup = document.getElementById('ac-popup');

let _acOpen = false;
let _acType = null;        // '@' or '/'
let _acTokenStart = -1;    // index of the trigger char in input.value
let _acTokenEnd = -1;      // caret position when the token was detected
let _acItems = [];         // [{kind, primary, secondary, insert, keepOpen}]
let _acActive = -1;
let _acSeq = 0;            // bumped to invalidate in-flight fetches
let _acDebounce = null;
let _acComposing = false;  // IME composition in progress
let _acCwd = '';

function _acDetect() {
    const caret = input.selectionStart;
    const left = input.value.slice(0, caret);
    // Slash command: only at the very start of the prompt (matches CLI
    // semantics and avoids false positives on shell paths like `ls /etc`).
    let m = left.match(/^(\s*)\/([^\s/]*)$/);
    if (m) return { type: '/', start: m[1].length, partial: m[2] };
    // File ref: at start or after whitespace; the path may contain slashes.
    m = left.match(/(^|\s)@([^\s]*)$/);
    if (m) return { type: '@', start: caret - m[2].length - 1, partial: m[2] };
    return null;
}

function _acOnInput() {
    if (_acComposing) return;
    const d = _acDetect();
    if (!d) { acClose(); return; }
    _acType = d.type;
    _acTokenStart = d.start;
    _acTokenEnd = input.selectionStart;
    const seq = ++_acSeq;
    if (_acDebounce) clearTimeout(_acDebounce);
    _acDebounce = setTimeout(() => _acFetch(d, seq), 120);
}

async function _acFetch(d, seq) {
    let items = [];
    try {
        if (d.type === '/') {
            const r = await fetch('/complete/skills?q=' + encodeURIComponent(d.partial));
            const data = await r.json();
            if (seq !== _acSeq) return;
            items = (data.skills || []).map(s => ({
                kind: 'skill',
                primary: '/' + s.name,
                secondary: s.description || '',
                insert: '/' + s.name + ' ',
                keepOpen: false,
            }));
        } else {
            const slash = d.partial.lastIndexOf('/');
            const dir = slash >= 0 ? d.partial.slice(0, slash + 1) : '';
            const base = (slash >= 0 ? d.partial.slice(slash + 1) : d.partial).toLowerCase();
            const r = await fetch('/complete/files?dir=' + encodeURIComponent(dir) +
                '&target=' + encodeURIComponent(getInputTarget() || ''));
            const data = await r.json();
            if (seq !== _acSeq) return;
            _acCwd = data.cwd || '';
            items = (data.entries || [])
                .filter(e => e.name.toLowerCase().startsWith(base))
                .map(e => ({
                    kind: e.type === 'dir' ? 'folder' : 'file',
                    primary: e.name + (e.type === 'dir' ? '/' : ''),
                    secondary: '',
                    insert: '@' + dir + e.name + (e.type === 'dir' ? '/' : ' '),
                    keepOpen: e.type === 'dir',
                }));
        }
    } catch (e) {
        return;
    }
    if (seq !== _acSeq) return;
    _acRender(items);
}

function _acRender(items) {
    _acItems = items;
    if (!items.length) { acClose(); return; }
    _acActive = 0;
    _acPopup.classList.toggle('ac-skills', _acType === '/');
    _acPopup.innerHTML = '';

    const section = document.createElement('div');
    section.className = 'ac-section';
    if (_acType === '/') {
        section.textContent = '/ skills';
    } else {
        const tail = (_acCwd || '').split('/').filter(Boolean).pop() || 'files';
        section.textContent = '@ ' + tail;
    }
    _acPopup.appendChild(section);

    items.forEach((it, i) => {
        const el = document.createElement('div');
        el.className = 'ac-item ac-' + it.kind + (i === 0 ? ' ac-active' : '');
        el.dataset.idx = i;
        const p = document.createElement('div');
        p.className = 'ac-primary';
        p.textContent = it.primary;
        el.appendChild(p);
        if (it.secondary) {
            const s = document.createElement('div');
            s.className = 'ac-secondary';
            s.textContent = it.secondary;
            el.appendChild(s);
        }
        _acPopup.appendChild(el);
    });

    _acPopup.classList.add('visible');
    _acOpen = true;
}

function _acMove(delta) {
    if (!_acItems.length) return;
    const rows = _acPopup.querySelectorAll('.ac-item');
    if (rows[_acActive]) rows[_acActive].classList.remove('ac-active');
    _acActive = (_acActive + delta + _acItems.length) % _acItems.length;
    const row = rows[_acActive];
    if (row) { row.classList.add('ac-active'); row.scrollIntoView({ block: 'nearest' }); }
}

function acActivate(i) {
    const it = _acItems[i];
    if (!it) return;
    const val = input.value;
    const before = val.slice(0, _acTokenStart);
    const after = val.slice(_acTokenEnd);
    input.value = before + it.insert + after;
    const pos = (before + it.insert).length;
    try { input.setSelectionRange(pos, pos); } catch (e) {}
    input.focus();
    if (it.keepOpen) {
        // Folder picked: drill in — re-detect from the new caret and re-fetch.
        _acOnInput();
    } else {
        acClose();
    }
}

// Called by app.js's trailing-newline poller so a mobile "send" keystroke
// accepts the highlighted suggestion instead of firing off the message.
function acConsumeEnter() {
    if (_acOpen && _acItems.length) {
        acActivate(_acActive >= 0 ? _acActive : 0);
        return true;
    }
    return false;
}

function acClose() {
    if (!_acOpen && !_acPopup.classList.contains('visible')) return;
    _acOpen = false;
    _acItems = [];
    _acActive = -1;
    _acSeq++;
    _acPopup.classList.remove('visible');
    _acPopup.innerHTML = '';
}

// --- wiring ---
input.addEventListener('input', _acOnInput);
input.addEventListener('compositionstart', () => { _acComposing = true; });
input.addEventListener('compositionend', () => { _acComposing = false; _acOnInput(); });
input.addEventListener('blur', () => { setTimeout(acClose, 150); });

// Registered before app.js's Enter handler (script load order), so when the
// popup is open we consume Enter/Tab before it can trigger a send.
input.addEventListener('keydown', function(e) {
    if (!_acOpen) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); _acMove(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); _acMove(-1); }
    else if (e.key === 'Enter' || e.key === 'Tab') {
        if (_acItems.length) {
            e.preventDefault();
            e.stopImmediatePropagation();
            acActivate(_acActive >= 0 ? _acActive : 0);
        }
    } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopImmediatePropagation();
        acClose();
    }
});

// pointerdown preventDefault keeps the textarea focused (no blur) when a row is
// tapped on mobile; the click handler does the actual selection.
_acPopup.addEventListener('pointerdown', e => { e.preventDefault(); });
_acPopup.addEventListener('click', e => {
    const item = e.target.closest('.ac-item');
    if (item) acActivate(parseInt(item.dataset.idx, 10));
});
