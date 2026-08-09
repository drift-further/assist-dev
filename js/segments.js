// segments.js — [handle] prompt segments.
//
// A segment is a favorite carrying a short handle. Typing [sol-dist] in the composer
// sends that favorite's whole body; the server does the substitution at send time
// (routes/input.py), so the textarea and the history entry keep the token form.
//
// The chips render inside the composer's own box rather than above it — a segment
// prompt must not cost a full-width band on a 390px screen.

// Deliberately no lookbehind: the escape check is done by hand below, which keeps this
// file parseable on older mobile Safari. Grammar mirrors shared/segments.py.
const SEG_TOKEN_RE = /\[([a-z0-9][a-z0-9._-]{1,31})\]/g;

const _segSheet = document.getElementById('seg-sheet');
const _segChips = document.getElementById('seg-chips');
const _inputWrap = document.querySelector('.input-wrap');

function segTokens(text) {
    const out = [];
    let m;
    SEG_TOKEN_RE.lastIndex = 0;
    while ((m = SEG_TOKEN_RE.exec(text || '')) !== null) {
        if (m.index > 0 && text[m.index - 1] === '\\') continue;  // \[x] is a literal
        out.push({ handle: m[1], index: m.index });
    }
    return out;
}

function segMap() {
    const map = {};
    for (const f of _favorites) {
        const h = (f.handle || '').trim().toLowerCase();
        if (h && !(h in map)) map[h] = f;
    }
    return map;
}

// Chips come from the already-loaded favorites, so typing stays offline-safe and
// costs no request. The eye/count preview asks the server for the authoritative text.
function renderSegChips() {
    if (!_segChips || !_inputWrap) return;
    const map = segMap();
    const seen = new Set();
    const chips = [];
    let chars = 0;

    for (const tok of segTokens(input.value)) {
        const fav = map[tok.handle];
        if (fav) chars += (fav.text || '').length;
        if (seen.has(tok.handle)) continue;
        seen.add(tok.handle);
        chips.push({ handle: tok.handle, known: !!fav });
    }

    if (!chips.length) {
        _inputWrap.classList.remove('has-segs');
        _segChips.innerHTML = '';
        return;
    }

    let html = '';
    for (const c of chips) {
        html += `<span class="seg-chip${c.known ? '' : ' unknown'}" data-handle="${escHtml(c.handle)}">`
             + `${escHtml(c.handle)}</span>`;
    }
    if (chars) html += `<span class="seg-count" id="seg-count">~${chars} ch</span>`;
    _segChips.innerHTML = html;
    _inputWrap.classList.add('has-segs');
}

// --- sheet ---

function segSheetClose() {
    if (_segSheet) _segSheet.classList.remove('visible');
}

function _segSheetOpen(title, bodyHtml, actionsHtml) {
    if (!_segSheet) return;
    _segSheet.innerHTML = `<div class="seg-sheet-inner">
        <div class="seg-sheet-head">
            <span class="seg-sheet-title">${title}</span>
            <button class="seg-sheet-close" onclick="segSheetClose()">&times;</button>
        </div>
        ${bodyHtml}
        ${actionsHtml || ''}
    </div>`;
    _segSheet.classList.add('visible');
}

function segPreview(handle) {
    const fav = segMap()[handle];
    if (!fav) {
        _segSheetOpen(`[${escHtml(handle)}]`,
            `<div class="seg-sheet-body">No segment with this handle — it will be sent exactly as typed.</div>`);
        return;
    }
    _segSheetOpen(`[${escHtml(handle)}]`,
        `<div class="seg-sheet-body">${escHtml(fav.text || '')}</div>`,
        `<div class="seg-sheet-actions">
            <button class="seg-btn" onclick="segEdit('${escHtml(fav.id)}')">Edit</button>
            <button class="seg-btn primary" onclick="segSheetClose()">Done</button>
        </div>`);
}

// The one authoritative read: whatever this returns is byte-for-byte what /type sends.
async function segPreviewAll() {
    _segSheetOpen('WILL SEND', '<div class="seg-sheet-body">Expanding…</div>');
    try {
        const resp = await fetch('/segments/expand', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: input.value}),
        });
        const data = await resp.json();
        const unknown = (data.tokens || []).filter(t => !t.known).map(t => t.handle);
        const warn = unknown.length
            ? `<div class="seg-sheet-err">Not a segment, sent literally: ${escHtml(unknown.join(', '))}</div>`
            : '';
        _segSheetOpen('WILL SEND',
            `<div class="seg-sheet-body">${escHtml(data.expanded || '')}</div>${warn}`,
            `<div class="seg-sheet-actions">
                <button class="seg-btn primary" onclick="segSheetClose()">Back</button>
            </div>`);
    } catch (e) {
        _segSheetOpen('WILL SEND', '<div class="seg-sheet-body">Offline — could not expand.</div>');
    }
}

function segEdit(id) {
    const fav = _favorites.find(f => f.id === id);
    if (!fav) return;
    _segSheetOpen(fav.handle ? `[${escHtml(fav.handle)}]` : 'NAME THIS BLOCK',
        `<div class="seg-sheet-body">
            <label class="seg-field-label">HANDLE — inserted as [handle]</label>
            <input class="seg-field" id="seg-edit-handle" value="${escHtml(fav.handle || '')}"
                   placeholder="sol-dist" autocapitalize="off" autocorrect="off" spellcheck="false">
            <label class="seg-field-label">BODY — what actually gets sent</label>
            <textarea class="seg-field" id="seg-edit-body" spellcheck="false">${escHtml(fav.text || '')}</textarea>
            <div class="seg-sheet-err" id="seg-edit-err"></div>
        </div>`,
        `<div class="seg-sheet-actions">
            <button class="seg-btn danger" onclick="segDelete('${escHtml(id)}')">Delete</button>
            <button class="seg-btn primary" onclick="segSave('${escHtml(id)}')">Save</button>
        </div>`);
}

async function segSave(id) {
    const handleEl = document.getElementById('seg-edit-handle');
    const bodyEl = document.getElementById('seg-edit-body');
    const errEl = document.getElementById('seg-edit-err');
    if (!handleEl || !bodyEl) return;
    try {
        const resp = await fetch('/favorite/' + encodeURIComponent(id), {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({handle: handleEl.value, text: bodyEl.value}),
        });
        const data = await resp.json();
        if (!data.ok) {
            if (errEl) errEl.textContent = data.error || 'Save failed';
            return;
        }
        segSheetClose();
        await loadHistory();
        renderSegChips();
        showFlash('sent', handleEl.value ? '[' + handleEl.value + '] saved' : 'Saved');
    } catch (e) {
        if (errEl) errEl.textContent = 'Offline';
    }
}

async function segDelete(id) {
    if (!confirm('Delete this favorite and its handle?')) return;
    try {
        await fetch('/favorite/' + encodeURIComponent(id), {method: 'DELETE'});
        segSheetClose();
        await loadHistory();
        renderSegChips();
    } catch (e) {}
}

// --- wiring ---

input.addEventListener('input', renderSegChips);

if (_segChips) {
    // pointerdown preventDefault keeps the textarea focused when a chip is tapped.
    _segChips.addEventListener('pointerdown', e => { e.preventDefault(); });
    _segChips.addEventListener('click', e => {
        if (e.target.closest('.seg-count')) { segPreviewAll(); return; }
        const chip = e.target.closest('.seg-chip');
        if (chip) segPreview(chip.dataset.handle);
    });
}

if (_segSheet) {
    _segSheet.addEventListener('click', e => {
        if (e.target === _segSheet) segSheetClose();
    });
}
