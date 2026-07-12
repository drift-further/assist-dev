// terminal.js — Terminal panel: projects, sessions, WebSocket, capture, code blocks

// -- ANSI color rendering --
const _ANSI_FG = [
    '#555555','#ff5555','#50fa7b','#f1fa8c',
    '#6272a4','#ff79c6','#8be9fd','#bbbbbb',
];
const _ANSI_FG_BRIGHT = [
    '#888888','#ff6e6e','#69ff94','#ffffa5',
    '#d6acff','#ff92df','#a4ffff','#ffffff',
];

function _ansi256(n) {
    if (n < 8) return _ANSI_FG[n];
    if (n < 16) return _ANSI_FG_BRIGHT[n - 8];
    if (n < 232) {
        n -= 16;
        const r = Math.floor(n / 36), g = Math.floor((n % 36) / 6), b = n % 6;
        return `rgb(${r ? r*40+55 : 0},${g ? g*40+55 : 0},${b ? b*40+55 : 0})`;
    }
    const v = (n - 232) * 10 + 8;
    return `rgb(${v},${v},${v})`;
}

function _stripOsc(text) {
    // Strip all OSC sequences: \x1b]...\x1b\ or \x1b]...\x07
    // Covers OSC 8 hyperlinks (tmux 3.4 emits id= params: \x1b]8;id=abc;<url>\x1b\)
    // and window title (OSC 0/1/2) and any other OSC sequences.
    return text.replace(/\x1b\][^\x1b\x07]*(?:\x1b\\|\x07)/g, '');
}

function stripAnsi(text) {
    return _stripOsc(text).replace(/\x1b\[[0-9;]*[A-Za-z]/g, '');
}

// Match http(s) URLs in raw (pre-escape) text. Stops at whitespace, angle
// brackets, or quotes — none of which belong inside a URL — so wrapping
// patterns like <url> or "url" stay outside the match.
const _URL_RE = /https?:\/\/[^\s<>"']+/g;
const _URL_TRAIL_RE = /[.,;:!?)\]}'"]+$/;

function _escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Escape a text segment to HTML, wrapping any http(s) URLs in clickable
// anchors that open in a new tab. Trailing punctuation (.,;:!?)]}'") is
// kept outside the link so "see https://x.com." renders correctly.
function _linkifyAndEscape(text) {
    let out = '';
    let last = 0;
    let m;
    _URL_RE.lastIndex = 0;
    while ((m = _URL_RE.exec(text)) !== null) {
        let url = m[0];
        const tm = url.match(_URL_TRAIL_RE);
        const tail = tm ? tm[0] : '';
        if (tail) url = url.slice(0, -tail.length);
        out += _escHtml(text.slice(last, m.index));
        const eu = _escHtml(url);
        out += `<a href="${eu}" target="_blank" rel="noopener noreferrer" class="term-link">${eu}</a>`;
        if (tail) out += _escHtml(tail);
        last = m.index + m[0].length;
    }
    out += _escHtml(text.slice(last));
    return out;
}

function ansiToHtml(rawText) {
    const parts = _stripOsc(rawText).split(/(\x1b\[[0-9;]*m)/);
    let html = '';
    let spanOpen = false;
    let fg = null, bg = null, bold = false, dim = false, italic = false, underline = false;

    for (const part of parts) {
        const sgr = part.match(/^\x1b\[([0-9;]*)m$/);
        if (sgr) {
            const codes = sgr[1] ? sgr[1].split(';').map(Number) : [0];
            let i = 0;
            while (i < codes.length) {
                const c = codes[i];
                if (c === 0) { fg = bg = null; bold = dim = italic = underline = false; }
                else if (c === 1) bold = true;
                else if (c === 2) dim = true;
                else if (c === 3) italic = true;
                else if (c === 4) underline = true;
                else if (c === 22) { bold = false; dim = false; }
                else if (c === 23) italic = false;
                else if (c === 24) underline = false;
                else if (c === 39) fg = null;
                else if (c === 49) bg = null;
                else if (c >= 30 && c <= 37) fg = _ANSI_FG[c - 30];
                else if (c >= 40 && c <= 47) bg = _ANSI_FG[c - 40];
                else if (c >= 90 && c <= 97) fg = _ANSI_FG_BRIGHT[c - 90];
                else if (c >= 100 && c <= 107) bg = _ANSI_FG_BRIGHT[c - 100];
                else if (c === 38 && i+1 < codes.length) {
                    if (codes[i+1] === 5 && i+2 < codes.length) { fg = _ansi256(codes[i+2]); i += 2; }
                    else if (codes[i+1] === 2 && i+4 < codes.length) { fg = `rgb(${codes[i+2]},${codes[i+3]},${codes[i+4]})`; i += 4; }
                }
                else if (c === 48 && i+1 < codes.length) {
                    if (codes[i+1] === 5 && i+2 < codes.length) { bg = _ansi256(codes[i+2]); i += 2; }
                    else if (codes[i+1] === 2 && i+4 < codes.length) { bg = `rgb(${codes[i+2]},${codes[i+3]},${codes[i+4]})`; i += 4; }
                }
                i++;
            }
            if (spanOpen) { html += '</span>'; spanOpen = false; }
            const s = [];
            if (fg) s.push('color:' + fg);
            if (bg) s.push('background:' + bg);
            if (bold) s.push('font-weight:bold');
            if (dim) s.push('opacity:0.7');
            if (italic) s.push('font-style:italic');
            if (underline) s.push('text-decoration:underline');
            if (s.length) { html += '<span style="' + s.join(';') + '">'; spanOpen = true; }
        } else {
            html += _linkifyAndEscape(part);
        }
    }
    if (spanOpen) html += '</span>';
    return html.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '');
}

// -- Projects --
async function loadProjects() {
    try {
        const resp = await fetch('/terminal/projects');
        const data = await resp.json();
        _termProjects = data.projects || [];
        renderProjects();
    } catch(e) {}
}

function shortName(name) {
    return name.replace(/^[-\w]+?_/, '');
}

// Agent color name → CSS color value
const _agentColors = {
    blue:   '#4fc3f7',
    green:  '#66bb6a',
    yellow: '#ffd54f',
    purple: '#ce93d8',
    red:    '#ef5350',
    cyan:   '#4dd0e1',
    orange: '#ffb74d',
    pink:   '#f48fb1',
};

function agentDisplayName(pane) {
    if (!pane.agent_name) return null;
    // Strip "-agent" suffix for compact display
    return pane.agent_name.replace(/-agent$/, '');
}

function _getRecentProjects() {
    try {
        const raw = localStorage.getItem('assist_recent_projects');
        if (!raw) return [];
        const entries = JSON.parse(raw);
        const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
        return entries.filter(e => e.ts > weekAgo).sort((a, b) => a.name.localeCompare(b.name));
    } catch(e) { return []; }
}

function _trackRecentProject(name) {
    try {
        const raw = localStorage.getItem('assist_recent_projects');
        let entries = raw ? JSON.parse(raw) : [];
        entries = entries.filter(e => e.name !== name);
        entries.push({ name, ts: Date.now() });
        // Keep last 20
        if (entries.length > 20) entries = entries.slice(-20);
        localStorage.setItem('assist_recent_projects', JSON.stringify(entries));
    } catch(e) {}
}

function _projButton(p) {
    let badges = '';
    if (p.venv) badges += '<span class="proj-badge venv">venv</span>';
    if (p.has_git) badges += '<span class="proj-badge git">git</span>';
    return `<button class="proj-btn" data-project="${escHtml(p.name)}">
        <span class="proj-name">${escHtml(shortName(p.name))}</span>
        <span class="proj-badges">${badges}</span>
    </button>`;
}

function renderProjects() {
    const filter = (document.getElementById('term-filter').value || '').toLowerCase();
    const grid = document.getElementById('term-project-grid');
    const filtered = _termProjects.filter(p => {
        if (!filter) return true;
        return p.name.toLowerCase().includes(filter) || shortName(p.name).toLowerCase().includes(filter);
    });

    // When filtering, show flat list (no sections)
    if (filter) {
        let html = '';
        for (const p of filtered) html += _projButton(p);
        grid.innerHTML = html || '<div class="term-empty">No matching projects</div>';
        for (const p of filtered.slice(0, 10)) loadProjectSessions(p.name);
        return;
    }

    const recent = _getRecentProjects();
    const recentNames = new Set(recent.map(r => r.name));
    const recentProjects = recent.map(r => filtered.find(p => p.name === r.name)).filter(Boolean);
    const otherProjects = filtered.filter(p => !recentNames.has(p.name));

    let html = '';
    if (recentProjects.length > 0) {
        html += '<div class="proj-section-label">Recent</div><div class="proj-section-grid">';
        for (const p of recentProjects) html += _projButton(p);
        html += '</div>';
    }
    if (otherProjects.length > 0) {
        html += '<div class="proj-section-label">All Projects</div><div class="proj-section-grid">';
        for (const p of otherProjects) html += _projButton(p);
        html += '</div>';
    }
    if (!html) {
        const recents = _getRecentExplores();
        if (recents.length > 0) {
            html += '<div class="proj-section-label">Recent Folders</div><div class="proj-section-grid">';
            for (const r of recents.slice(0, 12)) {
                const n = escHtml(r.folderName || r.path.split('/').pop());
                html += `<button class="proj-btn" data-path="${escHtml(r.path)}" data-folder="${escHtml(r.folderName || r.path.split('/').pop())}">
                    <span class="proj-name">${n}</span>
                    <span class="proj-badges"><span class="proj-badge" style="color:var(--text-muted);border-color:rgba(255,255,255,0.1)">${escHtml(r.path.replace(/\/[^/]+$/, '') || '/')}</span></span>
                </button>`;
            }
            html += '</div>';
        } else {
            html = '<div class="term-empty">No projects found</div>';
        }
    }
    grid.innerHTML = html;

    // One delegated click handler for every button in the grid — project
    // launch, path launch, and resume — so untrusted names/paths live only
    // in data-* attributes, never in interpolated inline handlers (XSS).
    if (!grid.dataset.delegated) {
        grid.dataset.delegated = '1';
        grid.addEventListener('click', (e) => {
            const resume = e.target.closest('.proj-session-btn');
            if (resume && grid.contains(resume)) {
                resumeSession(resume.dataset.sessionId, resume.dataset.project);
                return;
            }
            const btn = e.target.closest('.proj-btn');
            if (!btn || !grid.contains(btn)) return;
            if (btn.dataset.path !== undefined) {
                _launchFromPath(btn.dataset.path, btn.dataset.folder);
            } else if (btn.dataset.project !== undefined) {
                launchProject(btn.dataset.project, btn);
            }
        });
    }

    // Load session history for visible projects (non-blocking)
    const visibleProjects = [...recentProjects, ...otherProjects];
    for (const p of visibleProjects.slice(0, 10)) {
        loadProjectSessions(p.name);
    }
}

// Debounced — renderProjects() kicks off up to 10 session-history fetches,
// so firing it on every keystroke hammered the server and raced duplicate rows.
let _filterProjectsTimer = null;
function filterProjects() {
    if (_filterProjectsTimer) clearTimeout(_filterProjectsTimer);
    _filterProjectsTimer = setTimeout(() => {
        _filterProjectsTimer = null;
        renderProjects();
    }, 200);
}

// -- Session restore --
async function loadProjectSessions(projectName) {
    try {
        const resp = await fetch(`/terminal/sessions/history/${encodeURIComponent(projectName)}`);
        const data = await resp.json();
        if (!data.ok || !data.sessions.length) return;

        const container = document.querySelector(`.proj-btn[data-project="${projectName}"]`);
        if (!container) return;

        // Only show resumable sessions
        const resumable = data.sessions.filter(s => s.resumable);
        if (resumable.length === 0) return;

        let html = '<div class="proj-sessions">';
        for (const s of resumable) {
            const ago = _timeAgo(new Date(s.started_at));
            html += `<button class="proj-session-btn" data-session-id="${escHtml(s.session_id)}" data-project="${escHtml(projectName)}">
                Resume: ${ago}
            </button>`;
        }
        html += '</div>';
        // Stale responses can land after a re-render already inserted rows —
        // drop any existing sessions row before inserting.
        const existing = container.nextElementSibling;
        if (existing && existing.classList.contains('proj-sessions')) existing.remove();
        container.insertAdjacentHTML('afterend', html);
    } catch(e) {}
}

async function resumeSession(sessionId, projectName) {
    await launchProject(projectName);
    // Wait for session to be ready
    await new Promise(r => setTimeout(r, 1500));
    // Send claude --resume command
    await fetch('/type', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ text: CLAUDE_CMD + ' --resume ' + sessionId, enter: true, target: _termTarget }),
    });
    showFlash('sent', 'Resuming session...');
}

function _timeAgo(date) {
    const secs = Math.floor((Date.now() - date.getTime()) / 1000);
    if (secs < 60) return 'just now';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
    return Math.floor(secs / 86400) + 'd ago';
}

// -- Recent folder tracking (for native picker) --
function _trackRecentExplore(path, name) {
    try {
        const raw = localStorage.getItem('assist_recent_explores');
        let entries = raw ? JSON.parse(raw) : [];
        entries = entries.filter(e => e.path !== path);
        entries.push({path, folderName: name, ts: Date.now()});
        if (entries.length > 15) entries = entries.slice(-15);
        localStorage.setItem('assist_recent_explores', JSON.stringify(entries));
    } catch(e) {}
}

function _getRecentExplores() {
    try {
        const raw = localStorage.getItem('assist_recent_explores');
        if (!raw) return [];
        const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
        return JSON.parse(raw).filter(e => e.ts > weekAgo).sort((a, b) => b.ts - a.ts);
    } catch(e) { return []; }
}

// -- Native folder picker --
async function pickFolderNative() {
    const btn = document.getElementById('explore-btn');
    btn.disabled = true;
    btn.textContent = 'Picking...';
    try {
        const resp = await fetch('/terminal/explore/pick', {method: 'POST'});
        const data = await resp.json();
        if (!data.ok) {
            if (!data.cancelled) showFlash('error', data.error || 'Picker unavailable');
            return;
        }
        await _launchFromPath(data.path, data.name);
    } catch(e) {
        showFlash('error', 'Offline');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Browse';
    }
}

async function _launchFromPath(path, name) {
    try {
        const { cols, rows } = _calcTermSize();
        const resp = await fetch('/terminal/launch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project: name, cwd: path, cols, rows}),
        });
        const data = await resp.json();
        if (data.ok) {
            _trackRecentExplore(path, name);
            _termTarget = data.target;
            updateTmuxIndicator();
            try { localStorage.setItem('term_target', _termTarget); } catch(e) {}
            _termShowProjects = false;
            document.getElementById('term-projects').classList.add('hidden');
            document.getElementById('term-display').classList.remove('hidden');
            await loadSessions();
            await captureTerminal();
            startPolling();
            await fetch('/terminal/target', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({target: _termTarget}),
            });
            showFlash('sent', `Launched: ${name}`);
        } else {
            showFlash('error', data.error || 'Launch failed');
        }
    } catch(e) {
        showFlash('error', 'Offline');
    }
}

function toggleProjects() {
    _termShowProjects = !_termShowProjects;
    document.getElementById('term-projects').classList.toggle('hidden', !_termShowProjects);
}

async function launchProject(name, btnEl) {
    if (btnEl) btnEl.classList.add('launching');
    _trackRecentProject(name);
    try {
        const { cols, rows } = _calcTermSize();
        const resp = await fetch('/terminal/launch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project: name, cols, rows}),
        });
        const data = await resp.json();
        if (data.ok) {
            _termTarget = data.target;
            updateTmuxIndicator();
            try { localStorage.setItem('term_target', _termTarget); } catch(e) {}
            // Switch to terminal view
            _termShowProjects = false;
            document.getElementById('term-projects').classList.add('hidden');
            document.getElementById('term-display').classList.remove('hidden');
            // Refresh session list and start capture
            await loadSessions();
            await captureTerminal();
            startPolling();
            // Set target on server
            await fetch('/terminal/target', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({target: _termTarget}),
            });
        } else {
            showFlash('error', data.error || 'Launch failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
    if (btnEl) btnEl.classList.remove('launching');
}

// -- Sessions --
async function loadSessions() {
    try {
        const resp = await fetch('/terminal/sessions');
        const data = await resp.json();
        const panes = data.sessions || [];
        const container = document.getElementById('session-tabs');
        const current = _termTarget || data.active_target || '';

        // Store pane list for reference
        _sessionPanes = panes;

        container.innerHTML = '';
        if (panes.length === 0) {
            container.innerHTML = '<span class="session-tabs-empty">No sessions</span>';
            return;
        }

        // Group panes: detect teams (sessions with agent sub-panes)
        const teamSessions = new Set();
        for (const p of panes) {
            if (p.agent_name) teamSessions.add(p.session);
        }

        let prevSession = null;
        for (const p of panes) {
            const isAgent = !!p.agent_name;
            // Mirrors _applySessionsData() in app.js — keep the two in sync.
            const isSubpane = !isAgent && !!p.is_subpane;
            const isTeamLead = !isAgent && teamSessions.has(p.session);
            const aName = agentDisplayName(p);
            const aColor = _agentColors[p.agent_color] || '';

            // Insert team separator before first agent of a team group
            if (isTeamLead && prevSession !== p.session) {
                // No extra separator needed — lead tab starts the group
            }

            const tab = document.createElement('button');
            tab.className = 'session-tab';
            if (isAgent) tab.classList.add('agent-tab');
            if (isSubpane) tab.classList.add('agent-tab', 'subpane-tab');
            if (isTeamLead) tab.classList.add('team-lead-tab');
            tab.dataset.target = p.target;
            if (p.agent_name) tab.dataset.agentName = p.agent_name;

            // Label: agent name (short) or session·pane for unnamed sibling panes
            const label = aName || (isSubpane ? shortName(p.session) + '·' + p.pane : shortName(p.session));
            tab.textContent = label;

            // Agent color: left border accent
            if (isAgent && aColor) {
                tab.style.borderLeftWidth = '3px';
                tab.style.borderLeftColor = aColor;
            }

            // Attention dot
            const dot = document.createElement('span');
            dot.className = 'tab-dot';
            tab.appendChild(dot);

            // Restore prompt indicator if previously detected
            if (_sessionPrompts[p.target]) {
                tab.classList.add('has-prompt');
            }
            tab.onclick = function() { selectTab(p.target); };
            if (p.target === current) tab.classList.add('active');
            container.appendChild(tab);
            prevSession = p.session;
        }

        // Auto-select if we have a saved target
        if (current && panes.some(p => p.target === current)) {
            _termTarget = current;
        } else if (current && panes.length > 0) {
            // Viewed pane vanished (e.g. a subagent pane exited): fall back to
            // that session's first surviving pane rather than a dead target.
            // Mirrors _applySessionsData() in app.js — keep the two in sync.
            const sess = current.split(':')[0];
            const fallback = panes.find(p => p.session === sess) || panes[0];
            selectTab(fallback.target);
            return;
        } else if (panes.length > 0 && !_termTarget) {
            _termTarget = panes[0].target;
        }

        if (_termTarget) {
            markActiveTab(_termTarget);
            updateTmuxIndicator();
            document.getElementById('term-display').classList.remove('hidden');
        }
    } catch(e) {}
}

function markActiveTab(target) {
    const tabs = document.querySelectorAll('.session-tab');
    tabs.forEach(t => t.classList.toggle('active', t.dataset.target === target));
    // Clear prompt dot for the tab we're now viewing
    if (_sessionPrompts[target]) {
        delete _sessionPrompts[target];
        const tab = document.querySelector(`.session-tab[data-target="${CSS.escape(target)}"]`);
        if (tab) tab.classList.remove('has-prompt');
    }
}

let _tabSwitchScrollLock = false;  // briefly suppress scroll-freeze after tab switch

// Double-tap on the already-active tab triggers an aggressive pane clear
// (tmux clear-history + send-keys C-l). This is the only path that
// successfully cleans tmux-grid artifacts left by TUIs that don't repaint
// on SIGWINCH — typically a Claude running inside docker through claude-mount,
// where SIGWINCH on the host pane doesn't reach the in-container TUI.
let _lastTabTapTime = 0;
const _DOUBLE_TAP_MS = 500;

function selectTab(target) {
    if (target === _termTarget && (Date.now() - _lastTabTapTime) < _DOUBLE_TAP_MS) {
        _lastTabTapTime = 0;  // consume; require fresh sequence for next clear
        _clearActivePane(target);
        return;
    }
    _lastTabTapTime = Date.now();
    _termTarget = target;
    // Selecting a snoozed tab wakes it (covers any selection path).
    if (typeof _wakeSnoozed === 'function') _wakeSnoozed(target);
    markActiveTab(target);
    _termPaused = false;
    _tabSwitchScrollLock = true;  // suppress scroll-freeze until content renders
    _termLines = 2000;
    _termHasNew = false;
    _getSmartState(target).dismissedContent = null; // Reset dismiss so smart actions reappear on return
    // Drop any throttled render queued for the old target so it cannot fire
    // after the switch and paint stale content into term-content.
    if (_renderTimer) { clearTimeout(_renderTimer); _renderTimer = null; }
    _pendingRender = null;
    if (typeof clearSelection === 'function') clearSelection();
    document.getElementById('term-new-output').classList.remove('visible');
    updateTmuxIndicator();
    try { localStorage.setItem('term_target', target || ''); } catch(e) {}

    document.getElementById('term-display').classList.remove('hidden');
    // Hide projects panel when user explicitly selects a session tab
    _termShowProjects = false;
    document.getElementById('term-projects').classList.add('hidden');
    fetch('/terminal/target', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target: target}),
    });
    // Send new subscribe over existing WebSocket, or fall back to HTTP
    if (_termWs && _termWsConnected) {
        _termWs.send(JSON.stringify({
            type: 'subscribe',
            target: target,
            lines: _termLines,
        }));
    } else {
        captureTerminal();
        disconnectTerminalWs();
        connectTerminalWs();
    }
    // No auto-resize on tab click (per user): panes — TUI included — keep whatever
    // size they were launched/last fit at, so switching between devices never tugs a
    // shared tmux session. Fit deliberately via the tab's "Fit to screen" menu item
    // or the Fit button.
    // Scroll the active tab into view
    const activeTab = document.querySelector('.session-tab.active');
    if (activeTab) activeTab.scrollIntoView({behavior: 'smooth', inline: 'nearest', block: 'nearest'});

    // Show/hide command output overlay for this session
    onTabSwitchCommands(target.split(':')[0]);
    onTabSwitchSkills();
    // Sync auto-yes state from server and update UI for this session
    if (typeof syncAutoYesState === 'function') syncAutoYesState().then(() => updateAutoYesUI(target.split(':')[0]));
    if (typeof loadProjectSettings === 'function') loadProjectSettings(target.split(':')[0]);
}

// Kept for backward compat — tab clicks now use selectTab() directly
async function onSessionChange() {
    // No-op: tabs handle selection via selectTab()
}

async function _clearActivePane(target) {
    if (!target) return;
    try {
        await fetch('/terminal/clear', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target}),
        });
        showFlash('sent', 'Cleared');
        // Re-capture so the fresh state lands immediately.
        if (_termWs && _termWsConnected) {
            _termWs.send(JSON.stringify({type: 'subscribe', target, lines: _termLines}));
        } else {
            captureTerminal();
        }
    } catch(e) {
        showFlash('error', 'Clear failed');
    }
}

// -- Render throttle --
let _renderTimer = null;
let _lastRenderTime = 0;
let _pendingRender = null;  // latest content waiting for throttle to expire
const RENDER_MIN_INTERVAL = 200;   // ms — max ~5 renders/sec during streaming

// TUI mode: pane is on the alternate screen running a real app (not a shell
// left stuck on the alt screen by an uncleanly-exited TUI).
const _TUI_SHELLS = ['bash', 'zsh', 'sh', 'fish'];
let _paneTui = {};   // target -> bool
let _paneInfo = {};  // target -> last full-frame info dict

function _isTuiInfo(info) {
    if (!info || !info.alternate_on || !info.command) return false;
    if (_TUI_SHELLS.includes(info.command)) return false;
    // Claude Code panes are never TUI-managed even when they flip to the
    // alternate screen — deliberately-sized claude panes must not be auto-fit
    // (user decision 2026-07-10). command_display folds version-named
    // binaries (e.g. 2.1.206) into 'claude'.
    if ((info.command_display || info.command) === 'claude') return false;
    return true;
}

function _scheduleRender(content, info, target) {
    const now = Date.now();
    const elapsed = now - _lastRenderTime;

    if (elapsed >= RENDER_MIN_INTERVAL) {
        _pendingRender = null;
        _doRender(content, info, target);
    } else {
        // Always store latest content so we never drop the final update
        _pendingRender = { content, info, target };
        if (!_renderTimer) {
            _renderTimer = setTimeout(() => {
                _renderTimer = null;
                const p = _pendingRender;
                _pendingRender = null;
                if (p) _doRender(p.content, p.info, p.target);
            }, RENDER_MIN_INTERVAL - elapsed);
        }
    }
}

function _doRender(content, info, target) {
    _lastRenderTime = Date.now();

    // Update info bar
    if (info) {
        _paneInfo[target] = info;
        document.getElementById('term-info-cmd').textContent = info.command_display || info.command || '';
        document.getElementById('term-info-dim').textContent =
            (info.width && info.height) ? `${info.width}x${info.height}` : '';
        const isTui = _isTuiInfo(info);
        _paneTui[target] = isTui;
        if (target === _termTarget) {
            document.getElementById('term-tui-chip').classList.toggle('hidden', !isTui);
        }
    }

    // Update toggle status
    if (target) {
        const paneInfo = (_sessionPanes || []).find(p => p.target === target);
        const displayName = (paneInfo && paneInfo.agent_name)
            ? agentDisplayName(paneInfo)
            : shortName(target.split(':')[0]);
        document.getElementById('term-toggle-status').textContent = displayName;
    }

    // Content-based activity detection for background tabs
    if (target && content !== _termLastContent && _termLastContent !== '') {
        let tab = document.querySelector(`.session-tab[data-target="${CSS.escape(target)}"]`);
        // Tab may currently live in the stale sheet (no longer in DOM as a
        // .session-tab in the strip). If a stale row corresponds to this
        // target, re-evaluate the stale group so the tab is promoted back
        // into the strip immediately on activity.
        const staleRow = document.querySelector(`.stale-sheet-row[data-target="${CSS.escape(target)}"]`);
        if (!tab && staleRow && typeof _applyStaleGroup === 'function') {
            // Mark the underlying tab data so _applyStaleGroup will see it as running.
            // The next render cycle from server will recreate the tab DOM with
            // `running` set; here we just kick the re-evaluation.
            _applyStaleGroup();
            tab = document.querySelector(`.session-tab[data-target="${CSS.escape(target)}"]`);
        }
        if (tab && !tab.classList.contains('active')) {
            const wasRunning = tab.classList.contains('running');
            tab.classList.add('running');
            if (!wasRunning) {
                // NEW activity wakes a snoozed tab (see _applyStaleGroup).
                if (typeof _wakeSnoozed === 'function') _wakeSnoozed(target);
                if (typeof _applyStaleGroup === 'function') _applyStaleGroup();
            }
            if (_activityDecayTimers[target]) clearTimeout(_activityDecayTimers[target]);
            _activityDecayTimers[target] = setTimeout(() => {
                tab.classList.remove('running');
                delete _activityDecayTimers[target];
                if (typeof _applyStaleGroup === 'function') _applyStaleGroup();
            }, 5000);
        }
    }

    if (_termPaused) {
        if (content !== _termLastContent) {
            _termLatestContent = content;
            _termHasNew = true;
            document.getElementById('term-new-output').classList.add('visible');
        }
    } else {
        const pre = document.getElementById('term-content');
        const display = document.getElementById('term-display');
        pre.innerHTML = ansiToHtml(content);
        // Re-apply line selection highlights (survives innerHTML replacement)
        if (typeof applySelectionHighlights === 'function') applySelectionHighlights();
        _termLastContent = content;
        _termLatestContent = content;
        _termHasNew = false;
        // Guard programmatic scroll — prevent false scroll-freeze
        _autoScrolling = true;
        display.scrollTop = display.scrollHeight;
        requestAnimationFrame(() => { _autoScrolling = false; });
        // Release scroll lock after layout settles
        if (_tabSwitchScrollLock) {
            requestAnimationFrame(() => { _tabSwitchScrollLock = false; });
        }
    }

    // Show load-more if at line limit
    const lineCount = content.split('\n').length;
    document.getElementById('term-load-more').classList.toggle('visible', lineCount >= _termLines - 5);

    // Smart actions always run (user needs to respond to prompts quickly)
    const detected = detectSmartActions(stripAnsi(content), target || _termTarget);
    renderSmartActions(detected);
    _updateSudoSendBtn();
}

// -- WebSocket terminal streaming --
let _wsLastMessageTime = 0;      // timestamp of last WS message (capture or heartbeat)
let _wsInactivityTimer = null;    // timer to detect dead streams
const _WS_INACTIVITY_TIMEOUT = 8000;  // ms — reconnect if no message for this long

let _wsLatency = 0;  // latest round-trip time in ms
let _wsPingSentAt = 0;  // timestamp when ping was sent
let _wsPingTimer = null;  // single scheduled ping — cleared on reconnect/close so chains can't duplicate

// Timestamp markers — insert divider on >30s WS update gaps
let _lastWsUpdateTime = 0;
const _TIMESTAMP_GAP_SEC = 30;

// Exponential backoff for WS reconnection
let _wsReconnectDelay = 1000;        // current delay (starts at 1s)
const _WS_RECONNECT_MIN = 1000;      // 1s
function _getWsReconnectMax() { return SETTINGS ? SETTINGS.connection.ws_reconnect_max_ms : 30000; }
const _WS_RECONNECT_JITTER = 0.3;    // +/- 30% jitter

function _resetWsInactivityTimer() {
    _wsLastMessageTime = Date.now();
    if (_wsInactivityTimer) clearTimeout(_wsInactivityTimer);
    _wsInactivityTimer = setTimeout(_onWsInactive, _WS_INACTIVITY_TIMEOUT);
}

function _onWsInactive() {
    // No WS message in 8s — streamer likely died. Force reconnect.
    if (_termWsConnected && _termWs) {
        try { _termWs.close(); } catch(e) {}
    }
}

function connectTerminalWs() {
    if (_termWs) return;
    if (!_termTarget) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/terminal/stream`;
    let ws;
    try {
        ws = new WebSocket(url);
    } catch(e) {
        _fallbackToHttp();
        return;
    }
    _termWs = ws;
    // Handlers fire asynchronously — disconnect/reconnect (stopPolling →
    // startPolling) can replace _termWs before this socket's onclose runs.
    // Each handler bails unless it still owns the global, so a superseded
    // socket can't null the live one, start HTTP fallback, or schedule a
    // duplicate reconnect. disconnectTerminalWs() nulls _termWs before the
    // close event lands, which makes intentional closes stale here too.
    ws.onopen = function() {
        if (ws !== _termWs) return;
        _termWsConnected = true;
        _wsReconnectDelay = _WS_RECONNECT_MIN;  // reset backoff on success
        updateConnIndicator();
        // Send subscribe message
        ws.send(JSON.stringify({
            type: 'subscribe',
            target: _termTarget,
            lines: _termLines,
        }));
        // No auto-resize on (re)connect — repeated reconnects were resizing
        // the pane to transient viewport measurements. Resize is now manual
        // via the Fit menu only.
        // Measure real round-trip latency via ping (drop any chain from a
        // previous connection first so reconnects don't duplicate pings)
        if (_wsPingTimer) { clearTimeout(_wsPingTimer); _wsPingTimer = null; }
        _sendWsPing();
        // Clear HTTP polling since WS is active
        if (_termPollTimer) {
            clearInterval(_termPollTimer);
            _termPollTimer = null;
        }
        _resetWsInactivityTimer();
    };
    ws.onmessage = function(event) {
        if (ws !== _termWs) return;
        _resetWsInactivityTimer();
        onWsMessage(event);
    };
    ws.onclose = function() {
        if (ws !== _termWs) return;
        _termWsConnected = false;
        _termWs = null;
        if (_wsInactivityTimer) { clearTimeout(_wsInactivityTimer); _wsInactivityTimer = null; }
        if (_wsPingTimer) { clearTimeout(_wsPingTimer); _wsPingTimer = null; }
        updateConnIndicator();
        // Auto-reconnect with exponential backoff if terminal is still open
        if (_termOpen && _termTarget) {
            const jitter = 1 + (Math.random() * 2 - 1) * _WS_RECONNECT_JITTER;
            const delay = Math.min(_wsReconnectDelay * jitter, _getWsReconnectMax());
            _termWsReconnectTimer = setTimeout(function() {
                _termWsReconnectTimer = null;
                _wsReconnectDelay = Math.min(_wsReconnectDelay * 2, _getWsReconnectMax());
                if (_termOpen && _termTarget) connectTerminalWs();
            }, delay);
            // Fall back to HTTP polling in the meantime
            _fallbackToHttp();
        }
    };
    ws.onerror = function() {
        // onclose will fire after this
    };
}

function _sendWsPing() {
    if (_termWs && _termWsConnected) {
        _wsPingSentAt = Date.now();
        _termWs.send(JSON.stringify({type: 'ping'}));
    }
}

function onWsMessage(event) {
    try {
        const data = JSON.parse(event.data);
        if (data.type === 'pong' && _wsPingSentAt > 0) {
            _wsLatency = Date.now() - _wsPingSentAt;
            _wsPingSentAt = 0;
            updateConnIndicator();
            // Schedule next ping in 10s (single tracked timer)
            if (_wsPingTimer) clearTimeout(_wsPingTimer);
            _wsPingTimer = setTimeout(() => { _wsPingTimer = null; _sendWsPing(); }, 10000);
            return;
        }
        if (data.type === 'heartbeat') return;  // just resets inactivity timer (done in onmessage)
        if (data.type === 'autoyes') {
            handleAutoYesWsMessage(data);
            return;
        }
        if (data.type !== 'full' && data.type !== 'capture') return;
        // Timestamp marker on >30s gap
        const now = Date.now();
        if (_lastWsUpdateTime > 0 && (now - _lastWsUpdateTime) > _TIMESTAMP_GAP_SEC * 1000) {
            const date = new Date();
            const h = date.getHours();
            const m = String(date.getMinutes()).padStart(2, '0');
            const ampm = h >= 12 ? 'PM' : 'AM';
            const h12 = h % 12 || 12;
            const marker = `\n\u2500\u2500 ${h12}:${m} ${ampm} \u2500\u2500\n`;
            data.content = (data.content || '') + marker;
        }
        _lastWsUpdateTime = now;

        _applyTerminalContent(data.content || '', data.info, data.target);
    } catch(e) {}
}

function _applyTerminalContent(content, info, target) {
    // Reject stale content for wrong target
    if (target && _termTarget && target !== _termTarget) return;
    // Delegate to throttled renderer
    _scheduleRender(content, info, target);
}

function disconnectTerminalWs() {
    if (_termWsReconnectTimer) {
        clearTimeout(_termWsReconnectTimer);
        _termWsReconnectTimer = null;
    }
    if (_wsInactivityTimer) {
        clearTimeout(_wsInactivityTimer);
        _wsInactivityTimer = null;
    }
    if (_wsPingTimer) {
        clearTimeout(_wsPingTimer);
        _wsPingTimer = null;
    }
    if (_termWs) {
        _termWsConnected = false;
        try { _termWs.close(); } catch(e) {}
        _termWs = null;
    }
    updateConnIndicator();
}

function _fallbackToHttp() {
    if (!_termPollTimer && _termOpen && _termTarget) {
        // Delay HTTP fallback slightly so WS reconnect (1s) has a chance first
        setTimeout(function() {
            if (!_termWsConnected && !_termPollTimer && _termOpen && _termTarget) {
                captureTerminal();
                _termPollTimer = setInterval(captureTerminal, 3000);
                updateConnIndicator();
            }
        }, 1500);
    }
}

function updateConnIndicator() {
    const el = document.getElementById('conn-indicator');
    if (!el) return;
    if (_termWsConnected) {
        let bars, cls;
        if (_wsLatency < 200) { bars = '\u2582\u2584\u2586'; cls = 'conn-good'; }
        else if (_wsLatency < 1000) { bars = '\u2582\u2584'; cls = 'conn-ok'; }
        else { bars = '\u2582'; cls = 'conn-poor'; }
        el.textContent = bars;
        el.className = 'conn-indicator ' + cls;
    } else if (_termPollTimer) {
        el.textContent = '\u2582';
        el.className = 'conn-indicator conn-http';
    } else {
        el.textContent = '\u2717';
        el.className = 'conn-indicator conn-none';
    }
}

// -- Terminal capture (HTTP fallback) --
async function captureTerminal() {
    if (!_termTarget) return;
    try {
        const resp = await fetch(`/terminal/capture?target=${encodeURIComponent(_termTarget)}&lines=${_termLines}`);
        const data = await resp.json();
        if (!data.ok) return;
        _applyTerminalContent(data.content || '', data.info, data.target || _termTarget);
    } catch(e) {}
}

// -- Scroll freeze --
let _autoScrolling = false;  // true during programmatic scroll-to-bottom
(function() {
    const display = document.getElementById('term-display');
    display.addEventListener('scroll', function() {
        // Ignore scroll events caused by programmatic scroll, smart-actions layout shift, or tab switch
        if (_autoScrolling || _layoutShifting || _tabSwitchScrollLock) return;
        const atBottom = display.scrollHeight - display.scrollTop - display.clientHeight < 40;
        if (!atBottom && !_termPaused) {
            _termPaused = true;
        }
        if (atBottom && _termPaused) {
            _termPaused = false;
            _termHasNew = false;
            document.getElementById('term-new-output').classList.remove('visible');
        }
    });
})();

function resumeTerminal() {
    _termPaused = false;
    _termHasNew = false;
    document.getElementById('term-new-output').classList.remove('visible');
    // Apply latest content
    if (_termLatestContent) {
        const pre = document.getElementById('term-content');
        const display = document.getElementById('term-display');
        pre.innerHTML = ansiToHtml(_termLatestContent);
        _termLastContent = _termLatestContent;
        display.scrollTop = display.scrollHeight;
        // Re-detect smart actions after resume
        const detected = detectSmartActions(stripAnsi(_termLatestContent), _termTarget);
        renderSmartActions(detected);
    }
}

async function loadMore() {
    _termLines = Math.min(_termLines + 2000, 20000);
    if (!_termTarget) return;
    // Re-subscribe the WS with the new line count so the next streamed
    // frame doesn't erase the longer history we're about to render.
    if (_termWs && _termWsConnected) {
        _termWs.send(JSON.stringify({
            type: 'subscribe',
            target: _termTarget,
            lines: _termLines,
        }));
    }
    try {
        const resp = await fetch(`/terminal/capture?target=${encodeURIComponent(_termTarget)}&lines=${_termLines}`);
        const data = await resp.json();
        if (!data.ok) return;
        if (data.target && data.target !== _termTarget) return;
        const content = data.content || '';
        // Render directly, bypassing the paused check — the user is scrolled
        // up (that's always the case when tapping Load more). Anchor the
        // viewport so it doesn't jump when taller content lands.
        const pre = document.getElementById('term-content');
        const display = document.getElementById('term-display');
        const fromBottom = display.scrollHeight - display.scrollTop;
        _autoScrolling = true;
        pre.innerHTML = ansiToHtml(content);
        if (typeof applySelectionHighlights === 'function') applySelectionHighlights();
        _termLastContent = content;
        _termLatestContent = content;
        display.scrollTop = display.scrollHeight - fromBottom;
        requestAnimationFrame(() => { _autoScrolling = false; });
        const lineCount = content.split('\n').length;
        document.getElementById('term-load-more').classList.toggle('visible', lineCount >= _termLines - 5);
    } catch(e) {}
}

function showSessionInfo() {
    const toast = document.getElementById('session-info-toast');
    const text = document.getElementById('session-info-text');
    if (!_termTarget) {
        text.textContent = '(no active session)';
    } else {
        const session = _termTarget.split(':')[0];
        text.textContent = 'tmux attach -t ' + session;
    }
    toast.classList.add('visible');
    if (_infoTimer) clearTimeout(_infoTimer);
    _infoTimer = setTimeout(() => toast.classList.remove('visible'), 4000);
}

async function killSession() {
    if (!_termTarget) return;
    const session = _termTarget.split(':')[0];
    if (!confirm(`End session "${session}"?`)) return;
    try {
        const resp = await fetch('/terminal/kill', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({session: session}),
        });
        const data = await resp.json();
        if (data.ok) {
            // Clean up command split pane for this session
            onSessionKillCommands(session);
            // Clean up stale state for all panes of this session
            for (const key of Object.keys(_sessionStates)) {
                if (key.startsWith(session + ':')) {
                    delete _sessionStates[key];
                    delete _sessionPrompts[key];
                    delete _notifSentFor[key];
                    delete _lastScanContent[key];
                    delete _smartState[key];
                    if (_activityDecayTimers[key]) {
                        clearTimeout(_activityDecayTimers[key]);
                        delete _activityDecayTimers[key];
                    }
                }
            }
            _termTarget = null;
            _termPaused = false;
            _termHasNew = false;
            stopPolling();
            updateTmuxIndicator();
            try { localStorage.removeItem('term_target'); } catch(e) {}
            // Clear terminal display
            document.getElementById('term-content').textContent = '';
            document.getElementById('term-display').classList.add('hidden');
            document.getElementById('term-new-output').classList.remove('visible');
            document.getElementById('term-toggle-status').textContent = '';
            // Show projects
            _termShowProjects = true;
            document.getElementById('term-projects').classList.remove('hidden');
            // Refresh sessions
            loadSessions();
            // Clear server target
            fetch('/terminal/target', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({target: ''}),
            });
        }
    } catch(e) {
        showFlash('error', 'Offline');
    }
}

// -- Font size control --
let _termFontSize = parseInt(localStorage.getItem('assist_font_size')) || 13;
document.getElementById('term-content').style.fontSize = _termFontSize + 'px';

/** Measure monospace char width for current font size. */
function _measureCharWidth() {
    const el = document.getElementById('term-content');
    const span = document.createElement('span');
    span.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;font-family:inherit;font-size:' + _termFontSize + 'px';
    span.textContent = 'MMMMMMMMMM';
    el.appendChild(span);
    const w = span.offsetWidth / 10;
    el.removeChild(span);
    return w || (_termFontSize * 0.6);
}

/** Calculate cols/rows that fit the terminal viewport at current font size. */
function _calcTermSize() {
    const el = document.getElementById('term-content');
    const container = el.parentElement; // .term-display
    const charW = _measureCharWidth();
    const lineH = _termFontSize * 1.35;
    // Use container width (full available), subtract padding (8px each side).
    // Fall back to window size when container is hidden (clientWidth/Height = 0).
    const availW = (container && container.clientWidth > 0 ? container.clientWidth : window.innerWidth) - 16;
    const availH = (container && container.clientHeight > 0 ? container.clientHeight : window.innerHeight * 0.6) - 16;
    const cols = Math.max(40, Math.floor(availW / charW));
    // 60-row floor (not viewport-derived): claude's diff renderer misdraws
    // when its frame is taller than the pane, so keep panes tall even on
    // small phone viewports — the display scrolls anyway.
    const rows = Math.max(60, Math.floor(availH / lineH));
    return { cols, rows };
}

/** Exact viewport size for TUI panes — no 60-row floor (that floor exists
 *  for claude's diff renderer; a full-screen TUI must match the screen). */
function _calcTuiSize() {
    const el = document.getElementById('term-content');
    const container = el.parentElement;
    const charW = _measureCharWidth();
    const lineH = _termFontSize * 1.35;
    const availW = (container && container.clientWidth > 0 ? container.clientWidth : window.innerWidth) - 16;
    const availH = (container && container.clientHeight > 0 ? container.clientHeight : window.innerHeight * 0.6) - 16;
    return {
        cols: Math.max(40, Math.floor(availW / charW)),
        rows: Math.max(10, Math.floor(availH / lineH)),
    };
}

// Deliberate TUI fit. TUI panes never auto-fit — switching between devices must
// not tug a shared tmux session (an auto-fit on the phone would shrink the size the
// desktop is using). The user asks for a fit via the tab's "Fit to screen" menu item
// or the Fit button. Uses _calcTuiSize (exact viewport, no 60-row floor) and acts on
// the given tab's session even if a real client is attached — it's an explicit ask.
async function fitTuiToScreen(target) {
    target = target || _termTarget;
    if (!target || !_paneTui[target]) return;
    const session = target.split(':')[0];
    const { cols, rows } = _calcTuiSize();
    const targetCols = Math.max(40, Math.min(cols, 400));
    const targetRows = Math.max(10, Math.min(rows, 600));
    try {
        const resp = await fetch('/terminal/resize', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session, cols: targetCols, rows: targetRows }),
        });
        const data = await resp.json();
        if (data && data.ok && typeof showFlash === 'function') {
            showFlash('ok', `Fit to ${targetCols}×${targetRows}`);
        }
    } catch(e) {}
}

// TUI scroll forwarding: the capture is exactly one screen, so browser
// scrolling is meaningless — swipes/wheel page the TUI's own transcript.
// Taps and long-presses (text selection) pass through untouched.
let _tuiTouchY = null;
let _tuiWheelAcc = 0;
let _tuiLastScrollSend = 0;

function _tuiSendScroll(pageUp) {
    const now = Date.now();
    if (now - _tuiLastScrollSend < 120) return;  // cap the POST rate
    _tuiLastScrollSend = now;
    fetch('/key', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ keys: pageUp ? 'Page_Up' : 'Page_Down', target: _termTarget }),
    }).catch(function() {});
}

(function _wireTuiScroll() {
    const display = document.getElementById('term-display');
    display.addEventListener('wheel', function(e) {
        if (!_paneTui[_termTarget]) return;
        e.preventDefault();
        _tuiWheelAcc += e.deltaY;
        if (Math.abs(_tuiWheelAcc) >= 120) {
            _tuiSendScroll(_tuiWheelAcc < 0);
            _tuiWheelAcc = 0;
        }
    }, { passive: false });
    display.addEventListener('touchstart', function(e) {
        if (!_paneTui[_termTarget]) return;
        _tuiTouchY = e.touches[0].clientY;
        _tuiTapStartX = e.touches[0].clientX;
        _tuiTapStartY = e.touches[0].clientY;
    }, { passive: true });
    display.addEventListener('touchmove', function(e) {
        if (!_paneTui[_termTarget] || _tuiTouchY === null) return;
        // Don't page the transcript while selection.js is extending a
        // long-press selection: a completed hold sets _selDragging, and the
        // drag-to-extend gesture moves >80px, which would otherwise scroll.
        // (typeof guard matches the defensive cross-file idiom used above.)
        if (typeof _selDragging !== 'undefined' && _selDragging) return;
        e.preventDefault();
        const dy = e.touches[0].clientY - _tuiTouchY;
        if (Math.abs(dy) >= 80) {
            _tuiSendScroll(dy > 0);  // finger moving down = read older = PageUp
            _tuiTouchY = e.touches[0].clientY;
        }
    }, { passive: false });
    // Double-tap = jump to the end of the TUI's transcript (opencode binds
    // End to messages-last; verified live 2026-07-10). Swipe-paging back down
    // through a long transcript one page at a time was the phone pain point.
    let _tuiLastTap = 0, _tuiLastTapX = 0, _tuiLastTapY = 0;
    let _tuiTapStartX = 0, _tuiTapStartY = 0;
    display.addEventListener('touchend', function(e) {
        _tuiTouchY = null;
        if (!_paneTui[_termTarget]) return;
        if (typeof _selDragging !== 'undefined' && _selDragging) return;
        const t = e.changedTouches && e.changedTouches[0];
        if (!t) return;
        // A swipe is not a tap: the contact must have stayed put.
        if (Math.abs(t.clientX - _tuiTapStartX) > 20 ||
            Math.abs(t.clientY - _tuiTapStartY) > 20) { _tuiLastTap = 0; return; }
        const now = Date.now();
        const isDoubleTap = (now - _tuiLastTap) < 350 &&
            Math.abs(t.clientX - _tuiLastTapX) < 40 &&
            Math.abs(t.clientY - _tuiLastTapY) < 40;
        _tuiLastTap = now; _tuiLastTapX = t.clientX; _tuiLastTapY = t.clientY;
        if (!isDoubleTap) return;
        _tuiLastTap = 0;  // consume — a triple tap shouldn't fire twice
        fetch('/key', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ keys: 'End', target: _termTarget }),
        }).catch(function() {});
        if (typeof showFlash === 'function') showFlash('sent', 'Jump to end');
    });
})();

/** Resize the current tmux session.
 *  cols=null → auto width (fit viewport). cols=number → that width.
 *  rows=null → auto height (viewport, 60-row floor). rows=number → that exact height (min 10). */
async function termFitToScreen(cols = null, rows = null, opts = {}) {
    if (!_termTarget) return;
    const session = _termTarget.split(':')[0];
    const calc = _calcTermSize();
    const targetCols = cols == null ? calc.cols : Math.max(40, Math.min(parseInt(cols), 400));
    const targetRows = rows == null ? calc.rows : Math.max(10, Math.min(parseInt(rows), 600));
    try {
        const resp = await fetch('/terminal/resize', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session, cols: targetCols, rows: targetRows }),
        });
        const data = await resp.json();
        if (data.ok && !opts.quiet) {
            showFlash('ok', `Resized to ${targetCols}×${targetRows}`);
        }
    } catch(e) {}
}

/** Toggle the Fit preset menu. */
function toggleFitMenu() {
    const menu = document.getElementById('fit-menu');
    const btn = document.getElementById('btn-fit');
    const visible = menu.classList.contains('visible');
    menu.classList.toggle('visible', !visible);
    btn.classList.toggle('active', !visible);
    if (!visible) {
        // Pre-fill custom inputs with last values, highlight last preset
        const last = localStorage.getItem('assist_fit_last') || 'auto';
        const input = document.getElementById('fit-custom-input');
        if (input && /^\d+$/.test(last)) input.value = last;
        const lastRows = localStorage.getItem('assist_fit_rows');
        const rowsInput = document.getElementById('fit-custom-rows');
        if (rowsInput && lastRows && /^\d+$/.test(lastRows)) rowsInput.value = lastRows;
        document.querySelectorAll('.fit-item').forEach(el => {
            el.classList.toggle('active', el.dataset.cols === String(last));
        });
    }
}

/** Apply a preset choice from the Fit menu. */
function fitMenuPick(choice) {
    localStorage.setItem('assist_fit_last', String(choice));
    if (choice === 'auto') {
        termFitToScreen();
    } else {
        termFitToScreen(choice);
    }
    toggleFitMenu();
}

/** Apply the custom width and/or height from the Fit menu inputs.
 *  Each axis is independent: blank → auto for that dimension, filled → exact value. */
function fitMenuApplyCustom() {
    const colsRaw = document.getElementById('fit-custom-input').value.trim();
    const rowsRaw = document.getElementById('fit-custom-rows').value.trim();
    if (!colsRaw && !rowsRaw) {
        showFlash('error', 'Enter a width and/or height');
        return;
    }
    let cols = null, rows = null;
    if (colsRaw) {
        cols = parseInt(colsRaw);
        if (!cols || cols < 40 || cols > 400) {
            showFlash('error', 'Width must be 40–400');
            return;
        }
        localStorage.setItem('assist_fit_last', String(cols));
    }
    if (rowsRaw) {
        rows = parseInt(rowsRaw);
        if (!rows || rows < 10 || rows > 600) {
            showFlash('error', 'Height must be 10–600');
            return;
        }
        localStorage.setItem('assist_fit_rows', String(rows));
    }
    termFitToScreen(cols, rows);
    toggleFitMenu();
}

/** Unpin the current session's window-size so it follows the attached tmux client.
 *  Assist's Fit presets pin window-size=manual; this restores 'latest' so a real
 *  `tmux attach` from any terminal fits exactly with no padding dots. The next
 *  Fit re-pins it. */
async function fitMenuUnpin() {
    if (!_termTarget) {
        showFlash('error', 'No active session');
        toggleFitMenu();
        return;
    }
    const session = _termTarget.split(':')[0];
    try {
        const resp = await fetch('/terminal/unpin', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session }),
        });
        const data = await resp.json();
        showFlash(data.ok ? 'ok' : 'error',
            data.ok ? 'Window follows attached terminal' : (data.error || 'Unpin failed'));
    } catch(e) {
        showFlash('error', 'Unpin failed');
    }
    toggleFitMenu();
}

function termFontSmaller() {
    _termFontSize = Math.max(10, _termFontSize - 1);
    document.getElementById('term-content').style.fontSize = _termFontSize + 'px';
    try { localStorage.setItem('assist_font_size', _termFontSize); } catch(e) {}
    termFitToScreen();
}

function termFontLarger() {
    _termFontSize = Math.min(20, _termFontSize + 1);
    document.getElementById('term-content').style.fontSize = _termFontSize + 'px';
    try { localStorage.setItem('assist_font_size', _termFontSize); } catch(e) {}
    termFitToScreen();
}

// -- Terminal search --
let _searchMatches = [];
let _searchIndex = -1;
let _searchQueryLen = 0;

function toggleTermSearch() {
    const bar = document.getElementById('term-search');
    const isHidden = bar.classList.contains('hidden');
    bar.classList.toggle('hidden', !isHidden);
    if (isHidden) {
        document.getElementById('term-search-input').focus();
    } else {
        closeTermSearch();
    }
}

function closeTermSearch() {
    document.getElementById('term-search').classList.add('hidden');
    document.getElementById('term-search-input').value = '';
    document.getElementById('term-search-count').textContent = '';
    _searchMatches = [];
    _searchIndex = -1;
    // Remove highlights
    document.querySelectorAll('.search-highlight').forEach(el => {
        el.replaceWith(document.createTextNode(el.textContent));
    });
}

function onTermSearch() {
    const query = document.getElementById('term-search-input').value;
    const pre = document.getElementById('term-content');
    if (!query || !pre) {
        document.getElementById('term-search-count').textContent = '';
        _searchMatches = [];
        _searchIndex = -1;
        _searchQueryLen = 0;
        _clearSearchHighlight();
        return;
    }

    // Find matches in plain text
    const text = pre.textContent;
    const lowerQuery = query.toLowerCase();
    const lowerText = text.toLowerCase();
    _searchMatches = [];
    _searchQueryLen = query.length;
    let pos = 0;
    while (true) {
        const idx = lowerText.indexOf(lowerQuery, pos);
        if (idx < 0) break;
        _searchMatches.push(idx);
        pos = idx + 1;
    }

    _searchIndex = _searchMatches.length > 0 ? 0 : -1;
    _updateSearchCount();
    if (_searchIndex >= 0) _scrollToMatch(); else _clearSearchHighlight();
}

function _clearSearchHighlight() {
    document.querySelectorAll('.search-highlight').forEach(el => {
        const parent = el.parentNode;
        el.replaceWith(document.createTextNode(el.textContent));
        if (parent) parent.normalize();
    });
}

// Wrap the current match in a highlight span when it sits entirely inside
// one text node (the common case). Matches spanning ANSI span boundaries
// get scroll-only — wrapping across elements isn't worth the complexity.
function _highlightCurrentMatch(offset, length) {
    _clearSearchHighlight();
    const pre = document.getElementById('term-content');
    if (!pre || length <= 0) return;
    const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT);
    let pos = 0;
    let node;
    while ((node = walker.nextNode())) {
        const len = node.textContent.length;
        if (offset < pos + len) {
            if (offset >= pos && offset + length <= pos + len) {
                const range = document.createRange();
                range.setStart(node, offset - pos);
                range.setEnd(node, offset - pos + length);
                const mark = document.createElement('span');
                mark.className = 'search-highlight';
                mark.style.background = 'var(--amber)';
                mark.style.color = '#000';
                try { range.surroundContents(mark); } catch(e) {}
            }
            return;
        }
        pos += len;
    }
}

function _scrollToMatch() {
    if (_searchIndex < 0 || _searchIndex >= _searchMatches.length) return;
    const pre = document.getElementById('term-content');
    const display = document.getElementById('term-display');
    if (!pre || !display) return;
    const offset = _searchMatches[_searchIndex];
    const text = pre.textContent;
    // Line index = newline count before the match; content is white-space:pre
    // (no wrapping), so scrollHeight / totalLines gives the line height.
    const line = text.slice(0, offset).split('\n').length - 1;
    const totalLines = text.split('\n').length;
    const lineH = pre.scrollHeight / Math.max(1, totalLines);
    // Deliberately NOT guarded with _autoScrolling: the scroll listener
    // should pause the stream so the match stays in view.
    display.scrollTop = Math.max(0, line * lineH - display.clientHeight / 2);
    _highlightCurrentMatch(offset, _searchQueryLen);
}

function _updateSearchCount() {
    const el = document.getElementById('term-search-count');
    if (_searchMatches.length === 0) {
        el.textContent = '0/0';
    } else {
        el.textContent = (_searchIndex + 1) + '/' + _searchMatches.length;
    }
}

function termSearchPrev() {
    if (_searchMatches.length === 0) return;
    _searchIndex = (_searchIndex - 1 + _searchMatches.length) % _searchMatches.length;
    _updateSearchCount();
    _scrollToMatch();
}

function termSearchNext() {
    if (_searchMatches.length === 0) return;
    _searchIndex = (_searchIndex + 1) % _searchMatches.length;
    _updateSearchCount();
    _scrollToMatch();
}
