// studio.js — Studio attention inbox panel, connect sheet, ◇ badge, effort chip.
// Every Studio call is server-proxied through /studio/* so the API token
// never reaches the browser (effort 268 decision).

var _studioPanelOpen = false;
var _studioSheetOpen = false;
var _studioInbox = [];
var _studioState = 'unconfigured';
var _studioWebBase = '';

var _STUDIO_STATE_LABEL = {
    connected: '● connected',
    unreachable: '○ unreachable',
    unauthorized: '⚠ unauthorized',
    unconfigured: '○ not connected',
};

// ================================================================
// Panel
// ================================================================
function toggleStudioPanel() {
    _studioPanelOpen = !_studioPanelOpen;
    document.getElementById('studio-panel').classList.toggle('visible', _studioPanelOpen);
    if (_studioPanelOpen) loadStudioInbox();
}

async function loadStudioInbox() {
    try {
        var r = await fetch('/studio/inbox');
        var d = await r.json();
        _studioInbox = d.items || [];
        _studioState = d.state || 'unconfigured';
        _studioWebBase = d.web_base || '';
    } catch (e) {
        // Keep the last good queue. Blanking it would render "Nothing needs you
        // right now" over a queue that still has items, because _studioState may
        // still say connected.
    }
    renderStudioInbox();
}

function renderStudioInbox() {
    var stateEl = document.getElementById('studio-state');
    if (stateEl) {
        stateEl.textContent = _STUDIO_STATE_LABEL[_studioState] || _studioState;
        stateEl.className = 'studio-state ' + _studioState;
    }
    var body = document.getElementById('studio-body');
    if (!body) return;
    body.innerHTML = '';

    if (_studioState !== 'connected') {
        var note = document.createElement('div');
        note.className = 'studio-empty';
        if (_studioState === 'unauthorized') {
            note.textContent = 'Studio rejected the API token — update it in Settings → Studio.';
        } else if (_studioState === 'unreachable') {
            note.textContent = 'Studio is not answering. Showing the last known queue.';
        } else {
            note.textContent = 'Not connected to Studio. Tap ◇ Studio to connect.';
        }
        body.appendChild(note);
        if (_studioState === 'unconfigured') return;
    }

    if (!_studioInbox.length) {
        var empty = document.createElement('div');
        empty.className = 'studio-empty';
        empty.textContent = 'Nothing needs you right now.';
        body.appendChild(empty);
        return;
    }
    for (var i = 0; i < _studioInbox.length; i++) {
        body.appendChild(_studioItemEl(_studioInbox[i]));
    }
}

// Built with createElement + textContent throughout: every field below is
// Studio-authored text, and this panel must not be an HTML injection path.
function _studioItemEl(q) {
    var isRec = q.status === 'recommended';
    var item = document.createElement('div');
    item.className = 'studio-item' + (isRec ? ' rec' : '');
    item.dataset.qid = q.id;

    var meta = document.createElement('div');
    meta.className = 'studio-meta';
    var proj = document.createElement('span');
    proj.className = 'studio-proj';
    proj.textContent = (q.project_slug || '?') + ' · e' + q.effort_id;
    meta.appendChild(proj);
    meta.appendChild(document.createTextNode(
        ' · Q' + q.id + (q.task_id ? ' · task ' + q.task_id : '')));
    item.appendChild(meta);

    var prompt = document.createElement('div');
    prompt.className = 'studio-prompt';
    if (isRec) {
        var pill = document.createElement('span');
        pill.className = 'studio-rec-pill';
        pill.textContent = '✦ recommended';
        prompt.appendChild(pill);
    }
    prompt.appendChild(document.createTextNode(q.prompt || ''));
    item.appendChild(prompt);

    if (q.evidence) {
        var ev = document.createElement('div');
        ev.className = 'studio-evidence';
        ev.textContent = '▸ ' + q.evidence;
        item.appendChild(ev);
    }

    if (isRec && q.answer_json) {
        var rec = document.createElement('div');
        rec.className = 'studio-rec-text';
        var parts = [];
        if (q.answer_json.selections) parts.push(q.answer_json.selections.join(', '));
        if (q.answer_json.text) parts.push(q.answer_json.text);
        if (q.answer_json.rationale) parts.push('— ' + q.answer_json.rationale);
        rec.textContent = parts.join('  ');
        item.appendChild(rec);
    }

    var opts = document.createElement('div');
    opts.className = 'studio-opts';

    if (isRec) {
        var accept = document.createElement('button');
        accept.className = 'studio-accept';
        accept.textContent = 'Accept ✦';
        accept.addEventListener('click', function () {
            studioAnswer(q.id, 'accept', null, null);
        });
        opts.appendChild(accept);
    }

    var labels = Array.isArray(q.options_json) ? q.options_json : [];
    labels.forEach(function (label) {
        var b = document.createElement('button');
        b.className = 'studio-opt';
        b.textContent = label;
        b.addEventListener('click', function () {
            studioAnswer(q.id, 'answer', label, null);
        });
        opts.appendChild(b);
    });

    var free = document.createElement('input');
    free.className = 'studio-freetext';
    free.placeholder = 'or type an answer…';
    free.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && free.value.trim()) {
            e.preventDefault();
            studioAnswer(q.id, 'answer', null, free.value.trim());
        }
    });
    opts.appendChild(free);

    var link = document.createElement('a');
    link.className = 'studio-open-link';
    link.textContent = 'open in Studio ↗';
    link.href = (_studioWebBase || '') + '/#/p/' + q.project_id;
    link.target = '_blank';
    link.rel = 'noopener';
    opts.appendChild(link);

    item.appendChild(opts);
    return item;
}

async function studioAnswer(qid, action, select, text) {
    // A phone double-tap would otherwise fire twice: the second POST 404s (the
    // question is already out of the snapshot) and its error flash overwrites
    // the success one.
    var itemEl = document.querySelector('.studio-item[data-qid="' + qid + '"]');
    var controls = itemEl ? itemEl.querySelectorAll('button, input') : [];
    var reenable = function () {
        Array.prototype.forEach.call(controls, function (c) { c.disabled = false; });
    };
    Array.prototype.forEach.call(controls, function (c) { c.disabled = true; });
    var payload = { question_id: qid, action: action };
    if (select) payload.select = select;
    if (text) payload.text = text;
    try {
        var r = await fetch('/studio/answer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        var d = await r.json();
        if (d.ok) {
            showFlash('ok', action === 'accept' ? 'Recommendation accepted' : 'Answered Q' + qid);
            loadStudioInbox();
        } else {
            reenable();
            showFlash('error', d.error || 'Studio answer failed');
        }
    } catch (e) {
        reenable();
        showFlash('error', 'Offline');
    }
}

// ================================================================
// Poll integration — badge, button state, effort chip
// ================================================================
function _applyStudioData(block) {
    if (!block) return;
    _studioState = block.state || 'unconfigured';
    if (block.web_base) _studioWebBase = block.web_base;

    var btn = document.getElementById('btn-studio');
    var count = block.inbox_count || 0;
    if (btn) {
        btn.classList.toggle('grey', _studioState === 'unreachable' || _studioState === 'unconfigured');
        btn.classList.toggle('locked', _studioState === 'unauthorized');
        // Lock glyph and count badge are mutually exclusive: a rejected token
        // means the count on screen is stale, so don't show a number at all.
        var lock = document.getElementById('studio-lock');
        if (lock) lock.classList.toggle('hidden', _studioState !== 'unauthorized');
        var badge = document.getElementById('studio-badge');
        if (badge) {
            badge.textContent = count > 9 ? '9+' : String(count);
            badge.classList.toggle('hidden', !(count > 0 && _studioState === 'connected'));
        }
    }
    // Re-read the snapshot on EVERY poll while the panel is open — not only
    // when the count changes. An outage preserves inbox_count (the server keeps
    // serving the last snapshot), so a count-gated refresh would leave the
    // header reading "connected" for the entire outage. This is an Assist-local
    // read; the refresher thread still owns every call to Studio itself.
    if (_studioPanelOpen) loadStudioInbox();
    updateStudioChip(block);
}

function updateStudioChip(block) {
    var chip = document.getElementById('ci-effort');
    if (!chip) return;
    if (_studioState !== 'connected' || (!block.project_id && !block.effort)) {
        chip.classList.add('hidden');
        chip.textContent = '';
        return;
    }
    var label = '◇ ' + (block.project_name || 'Studio');
    if (block.effort) label += ' · e' + block.effort;
    chip.textContent = label;
    chip.classList.remove('hidden');
    chip.onclick = function () {
        if (!block.project_id) return;
        window.open((_studioWebBase || '') + '/#/p/' + block.project_id, '_blank', 'noopener');
    };
}

// ================================================================
// Connect sheet — the free -> connected doorway
// ================================================================
function openStudioConnect() {
    _studioSheetOpen = true;
    var sheet = document.getElementById('studio-sheet');
    sheet.classList.add('visible');
    // The markup ships aria-hidden="true"; leaving it set would keep the open
    // sheet invisible to assistive tech.
    sheet.setAttribute('aria-hidden', 'false');
    var s = (window.SETTINGS && SETTINGS.studio) || {};
    document.getElementById('studio-web-base').value = s.web_base || '';
    document.getElementById('studio-api-base').value = s.api_base || '';
    document.getElementById('studio-api-token').value = '';   // never seed the mask
    document.getElementById('studio-connect-result').textContent = '';
}

function closeStudioConnect() {
    _studioSheetOpen = false;
    var sheet = document.getElementById('studio-sheet');
    sheet.classList.remove('visible');
    sheet.setAttribute('aria-hidden', 'true');
}

async function studioConnectSave() {
    var out = document.getElementById('studio-connect-result');
    var patch = { studio: {
        web_base: document.getElementById('studio-web-base').value.trim(),
        api_base: document.getElementById('studio-api-base').value.trim(),
    } };
    var tok = document.getElementById('studio-api-token').value.trim();
    if (tok) patch.studio.api_token = tok;
    out.textContent = 'Testing…';
    try {
        var r = await fetch('/api/settings', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        var d = await r.json();
        if (!d.ok) { out.textContent = d.error || 'Save failed'; return; }
        SETTINGS = d.settings;
        var t = await fetch('/studio/test', { method: 'POST' });
        var td = await t.json();
        _studioState = td.state;
        out.textContent = _STUDIO_STATE_LABEL[td.state] || td.state;
        if (td.state === 'connected') {
            showFlash('ok', 'Studio connected');
            closeStudioConnect();
            loadStudioInbox();
        }
    } catch (e) {
        out.textContent = 'Offline';
    }
}

async function studioTestConnection() {
    try {
        var r = await fetch('/studio/test', { method: 'POST' });
        var d = await r.json();
        _studioState = d.state;
        showFlash(d.state === 'connected' ? 'ok' : 'error',
            'Studio: ' + (_STUDIO_STATE_LABEL[d.state] || d.state));
        if (_studioPanelOpen) loadStudioInbox();
    } catch (e) {
        showFlash('error', 'Offline');
    }
}
