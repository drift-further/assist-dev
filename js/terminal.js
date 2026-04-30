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
            html += part.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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
    return `<button class="proj-btn" data-project="${escHtml(p.name)}" onclick="launchProject('${escHtml(p.name).replace(/'/g, "\\'")}', this)">
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
                const p = escHtml(r.path), n = escHtml(r.folderName || r.path.split('/').pop());
                html += `<button class="proj-btn" onclick="_launchFromPath(${escHtml(JSON.stringify(r.path))}, ${escHtml(JSON.stringify(r.folderName || r.path.split('/').pop()))})">
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

    // Load session history for visible projects (non-blocking)
    const visibleProjects = [...recentProjects, ...otherProjects];
    for (const p of visibleProjects.slice(0, 10)) {
        loadProjectSessions(p.name);
    }
}

function filterProjects() {
    renderProjects();
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
            html += `<button class="proj-session-btn" onclick="resumeSession('${s.session_id}', '${projectName.replace(/'/g, "\\'")}')">
                Resume: ${ago}
            </button>`;
        }
        html += '</div>';
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
            if (isTeamLead) tab.classList.add('team-lead-tab');
            tab.dataset.target = p.target;
            if (p.agent_name) tab.dataset.agentName = p.agent_name;

            // Label: agent name (short) or session name
            const label = aName || shortName(p.session);
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

function selectTab(target) {
    _termTarget = target;
    markActiveTab(target);
    _termPaused = false;
    _tabSwitchScrollLock = true;  // suppress scroll-freeze until content renders
    _termLines = 2000;
    _termLineBuffer = [];  // reset delta buffer on tab switch
    _termHasNew = false;
    _smartDismissed = null; // Reset dismiss so smart actions reappear on return
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

// -- Render throttle --
let _renderTimer = null;
let _lastRenderTime = 0;
let _pendingRender = null;  // latest content waiting for throttle to expire
const RENDER_MIN_INTERVAL = 200;   // ms — max ~5 renders/sec during streaming

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
    // Guard: a queued render may carry the previous target if the user
    // switched tabs during the throttle window. selectTab clears the timer,
    // but defense-in-depth here keeps stale content out of term-content.
    if (target && _termTarget && target !== _termTarget) return;
    _lastRenderTime = Date.now();

    // Update info bar
    if (info) {
        document.getElementById('term-info-cmd').textContent = info.command || '';
        document.getElementById('term-info-dim').textContent =
            (info.width && info.height) ? `${info.width}x${info.height}` : '';
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
            if (!wasRunning && typeof _applyStaleGroup === 'function') _applyStaleGroup();
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
    const detected = detectSmartActions(stripAnsi(content));
    renderSmartActions(detected);
    _updateSudoSendBtn();
}

// -- WebSocket terminal streaming --
let _wsLastMessageTime = 0;      // timestamp of last WS message (capture or heartbeat)
let _wsInactivityTimer = null;    // timer to detect dead streams
const _WS_INACTIVITY_TIMEOUT = 8000;  // ms — reconnect if no message for this long

let _termLineBuffer = [];  // internal line buffer for delta reconstruction
let _wsLatency = 0;  // latest round-trip time in ms
let _wsPingSentAt = 0;  // timestamp when ping was sent

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
    try {
        _termWs = new WebSocket(url);
    } catch(e) {
        _fallbackToHttp();
        return;
    }
    _termWs.onopen = function() {
        _termWsConnected = true;
        _wsReconnectDelay = _WS_RECONNECT_MIN;  // reset backoff on success
        updateConnIndicator();
        // Send subscribe message
        _termWs.send(JSON.stringify({
            type: 'subscribe',
            target: _termTarget,
            lines: _termLines,
        }));
        // Measure real round-trip latency via ping
        _sendWsPing();
        // Clear HTTP polling since WS is active
        if (_termPollTimer) {
            clearInterval(_termPollTimer);
            _termPollTimer = null;
        }
        _resetWsInactivityTimer();
    };
    _termWs.onmessage = function(event) {
        _resetWsInactivityTimer();
        onWsMessage(event);
    };
    _termWs.onclose = function() {
        _termWsConnected = false;
        _termWs = null;
        if (_wsInactivityTimer) { clearTimeout(_wsInactivityTimer); _wsInactivityTimer = null; }
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
    _termWs.onerror = function() {
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
            // Schedule next ping in 10s
            setTimeout(_sendWsPing, 10000);
            return;
        }
        if (data.type === 'heartbeat') return;  // just resets inactivity timer (done in onmessage)
        if (data.type === 'autoyes') {
            handleAutoYesWsMessage(data);
            return;
        }
        // Reject stale content for wrong target BEFORE modifying line buffer.
        // Race: streamer may send for old target after tab switch but before
        // the server processes the new subscribe. Without this guard the buffer
        // gets corrupted and subsequent deltas render mixed-session content.
        if (data.target && _termTarget && data.target !== _termTarget) return;
        // Timestamp marker on >30s gap
        const now = Date.now();
        if (_lastWsUpdateTime > 0 && (now - _lastWsUpdateTime) > _TIMESTAMP_GAP_SEC * 1000) {
            const date = new Date();
            const h = date.getHours();
            const m = String(date.getMinutes()).padStart(2, '0');
            const ampm = h >= 12 ? 'PM' : 'AM';
            const h12 = h % 12 || 12;
            const marker = `\n\u2500\u2500 ${h12}:${m} ${ampm} \u2500\u2500\n`;
            if (data.type === 'full' || data.type === 'capture') {
                data.content = (data.content || '') + marker;
            }
        }
        _lastWsUpdateTime = now;

        if (data.type === 'full') {
            _termLineBuffer = (data.content || '').split('\n');
            _applyTerminalContent(data.content || '', data.info, data.target);
        } else if (data.type === 'delta') {
            for (const op of (data.ops || [])) {
                if (op.op === 'append') {
                    _termLineBuffer = _termLineBuffer.concat(op.lines);
                } else if (op.op === 'replace') {
                    _termLineBuffer = _termLineBuffer.slice(0, op.start).concat(op.lines);
                }
            }
            const content = _termLineBuffer.join('\n');
            _applyTerminalContent(content, data.info, data.target);
        } else if (data.type === 'capture') {
            // Legacy compatibility
            _applyTerminalContent(data.content || '', data.info, data.target);
        } else {
            return;
        }
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
        const detected = detectSmartActions(stripAnsi(_termLatestContent));
        renderSmartActions(detected);
    }
}

function loadMore() {
    _termLines = Math.min(_termLines + 2000, 20000);
    captureTerminal();
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
    const rows = Math.max(10, Math.floor(availH / lineH));
    return { cols, rows };
}

/** Resize the current tmux session.
 *  cols=null → fit to viewport (Auto). cols=number → use that width, height from viewport. */
async function termFitToScreen(cols = null) {
    if (!_termTarget) return;
    const session = _termTarget.split(':')[0];
    const calc = _calcTermSize();
    const targetCols = cols == null ? calc.cols : Math.max(40, Math.min(parseInt(cols), 400));
    const targetRows = calc.rows;
    try {
        const resp = await fetch('/terminal/resize', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ session, cols: targetCols, rows: targetRows }),
        });
        const data = await resp.json();
        if (data.ok) {
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
        // Pre-fill custom input with last value, highlight last preset
        const last = localStorage.getItem('assist_fit_last') || 'auto';
        const input = document.getElementById('fit-custom-input');
        if (input && /^\d+$/.test(last)) input.value = last;
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

/** Apply the custom column count from the Fit menu input. */
function fitMenuApplyCustom() {
    const input = document.getElementById('fit-custom-input');
    const v = parseInt(input.value);
    if (!v || v < 40 || v > 400) {
        showFlash('error', 'Width must be 40–400');
        return;
    }
    localStorage.setItem('assist_fit_last', String(v));
    termFitToScreen(v);
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
        return;
    }

    // Find matches in plain text
    const text = pre.textContent;
    const lowerQuery = query.toLowerCase();
    const lowerText = text.toLowerCase();
    _searchMatches = [];
    let pos = 0;
    while (true) {
        const idx = lowerText.indexOf(lowerQuery, pos);
        if (idx < 0) break;
        _searchMatches.push(idx);
        pos = idx + 1;
    }

    _searchIndex = _searchMatches.length > 0 ? 0 : -1;
    _updateSearchCount();
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
}

function termSearchNext() {
    if (_searchMatches.length === 0) return;
    _searchIndex = (_searchIndex + 1) % _searchMatches.length;
    _updateSearchCount();
}
