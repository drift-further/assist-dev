// commands.js — Per-project saved commands + skills + split-pane output overlay

// ================================================================
// Skills Panel — load project .claude/skills, paste into input
// ================================================================

let _skillsPanelOpen = false;
let _currentSkills = [];
let _currentSkillsProject = '';

function toggleSkillsPanel() {
    _skillsPanelOpen = !_skillsPanelOpen;
    const panel = document.getElementById('skills-panel');
    panel.classList.toggle('visible', _skillsPanelOpen);
    if (_skillsPanelOpen) loadSkills();
}

async function loadSkills() {
    if (!_termTarget) {
        _currentSkills = [];
        _currentSkillsProject = '';
        renderSkills();
        return;
    }
    const session = _termTarget.split(':')[0];
    // Only re-fetch if project changed
    if (session === _currentSkillsProject && _currentSkills.length > 0) {
        renderSkills();
        return;
    }
    _currentSkillsProject = session;
    try {
        const resp = await fetch(`/api/skills/${encodeURIComponent(session)}`);
        const data = await resp.json();
        _currentSkills = data.ok ? (data.skills || []) : [];
    } catch (e) {
        _currentSkills = [];
    }
    renderSkills();
}

function renderSkills() {
    const list = document.getElementById('skills-list');
    if (_currentSkills.length === 0) {
        list.innerHTML = '<div class="skills-empty">No skills found</div>';
        return;
    }
    // Separate project vs global, sort each alphabetically
    const project = _currentSkills.filter(s => s.source !== 'global').sort((a, b) => a.name.localeCompare(b.name));
    const global = _currentSkills.filter(s => s.source === 'global').sort((a, b) => a.name.localeCompare(b.name));
    let html = '';
    const renderItem = (s) => `<button class="skill-item" onclick="selectSkill('${escHtml(s.name).replace(/'/g, "\\'")}')">
            <span class="skill-slash">/</span>
            <div class="skill-info">
                <span class="skill-name">${escHtml(s.name)}</span>
                <span class="skill-desc">${escHtml(s.description)}</span>
            </div>
        </button>`;
    if (project.length) {
        if (global.length) html += '<div class="skills-section-label">Project</div>';
        for (const s of project) html += renderItem(s);
    }
    if (global.length) {
        html += '<div class="skills-section-label">Global</div>';
        for (const s of global) html += renderItem(s);
    }
    list.innerHTML = html;
}

function selectSkill(name) {
    // Paste /<name> into input box without sending
    input.value = '/' + name + ' ';
    input.focus();
    // Close panel
    _skillsPanelOpen = false;
    document.getElementById('skills-panel').classList.remove('visible');
    showFlash('sent', '/' + name);
}

// Reset skills cache + close panel on tab switch
function onTabSwitchSkills() {
    _currentSkillsProject = '';
    _currentSkills = [];
    if (_skillsPanelOpen) {
        _skillsPanelOpen = false;
        document.getElementById('skills-panel').classList.remove('visible');
    }
    document.getElementById('skills-list').innerHTML = '';
}

// ================================================================
// Command Panel — load, render, run
// ================================================================

function toggleCmdPanel() {
    _cmdPanelOpen = !_cmdPanelOpen;
    const panel = document.getElementById('cmd-panel');
    panel.classList.toggle('visible', _cmdPanelOpen);
    if (_cmdPanelOpen) loadCommands();
}

async function loadCommands() {
    if (!_termTarget) {
        _currentCommands = [];
        _currentCommandsProject = '';
        renderCmdPanel();
        return;
    }
    const session = _termTarget.split(':')[0];
    // Only re-fetch if project changed
    if (session === _currentCommandsProject && _currentCommands.length > 0) {
        renderCmdPanel();
        return;
    }
    _currentCommandsProject = session;
    try {
        const resp = await fetch(`/api/commands/${encodeURIComponent(session)}`);
        const data = await resp.json();
        _currentCommands = data.ok ? (data.commands || []) : [];
    } catch (e) {
        _currentCommands = [];
    }
    renderCmdPanel();
}

const CMD_ICONS = {
    test: '\u{1F9EA}',    // test tube
    run: '\u25B6',        // play
    build: '\u{1F528}',   // hammer
    lint: '\u2728',       // sparkles
    check: '\u2705',      // check mark
    deploy: '\u{1F680}',  // rocket
    clean: '\u{1F9F9}',   // broom
};

function renderCmdPanel() {
    const list = document.getElementById('cmd-list');
    if (_currentCommands.length === 0) {
        list.innerHTML = '<div class="cmd-empty">No commands. Add .assist-commands.json to project root.</div>';
        return;
    }
    let html = '';
    for (let i = 0; i < _currentCommands.length; i++) {
        const c = _currentCommands[i];
        const icon = CMD_ICONS[c.icon] || CMD_ICONS.run;
        html += `<button class="cmd-item" onclick="runSavedCommand(${i})">
            <span class="cmd-item-icon">${icon}</span>
            <div class="cmd-item-info">
                <div class="cmd-item-name">${escHtml(c.name)}</div>
                <div class="cmd-item-cmd">${escHtml(c.cmd)}</div>
            </div>
        </button>`;
    }
    list.innerHTML = html;
}

async function runSavedCommand(index) {
    const cmd = _currentCommands[index];
    if (!cmd || !_termTarget) return;

    const session = _termTarget.split(':')[0];

    // Close command panel
    _cmdPanelOpen = false;
    document.getElementById('cmd-panel').classList.remove('visible');

    showFlash('sent', 'Running: ' + cmd.name);

    try {
        const resp = await fetch('/api/commands/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session: session,
                cmd: cmd.cmd,
                project: _currentCommandsProject,
            }),
        });
        const data = await resp.json();
        if (data.ok) {
            // Track split pane state
            _splitPanes[session] = {
                target: data.target,
                ws: null,
                wsConnected: false,
                lastContent: '',
                label: cmd.name,
            };
            openCmdOutput(session);
        } else {
            showFlash('error', data.error || 'Run failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

// ================================================================
// Output Overlay — WebSocket streaming from split pane
// ================================================================

function openCmdOutput(session) {
    const state = _splitPanes[session];
    if (!state) return;

    _cmdOutputOpen = true;
    const overlay = document.getElementById('cmd-output');
    const label = document.getElementById('cmd-output-label');
    const dot = document.getElementById('cmd-output-dot');
    const pre = document.getElementById('cmd-output-pre');

    label.textContent = state.label || 'Command';
    dot.classList.remove('stopped');
    pre.textContent = state.lastContent || '';
    overlay.classList.add('visible');

    connectSplitWs(session);
}

function closeCmdOutput() {
    _inputToSplit = false;
    updateInputToggleUI();
    updateTmuxIndicator();

    if (!_termTarget) return;
    const session = _termTarget.split(':')[0];
    const state = _splitPanes[session];

    _cmdOutputOpen = false;
    document.getElementById('cmd-output').classList.remove('visible');

    // Kill split pane
    if (state) {
        disconnectSplitWs(session);
        fetch('/api/commands/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session: session }),
        }).catch(() => {});
        delete _splitPanes[session];
    }
}

function connectSplitWs(session) {
    const state = _splitPanes[session];
    if (!state || state.ws) return;

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/terminal/stream`;
    try {
        state.ws = new WebSocket(url);
    } catch (e) {
        return;
    }

    state.ws.onopen = function () {
        state.wsConnected = true;
        state.ws.send(JSON.stringify({
            type: 'subscribe',
            target: state.target,
            lines: 200,
        }));
    };

    state.ws.onmessage = function (event) {
        try {
            const data = JSON.parse(event.data);
            if (data.type !== 'capture') return;
            state.lastContent = data.content || '';
            const currentSession = _termTarget ? _termTarget.split(':')[0] : '';
            if (_cmdOutputOpen && currentSession === session) {
                const pre = document.getElementById('cmd-output-pre');
                const container = document.getElementById('cmd-output-content');
                pre.textContent = state.lastContent;
                container.scrollTop = container.scrollHeight;
                // Detect smart actions on split pane content
                const detected = detectSmartActions(stripAnsi(state.lastContent));
                if (detected) renderSmartActions(detected, state.target);
            }
        } catch (e) {}
    };

    state.ws.onclose = function () {
        state.wsConnected = false;
        state.ws = null;
        // Mark dot as stopped (pane may have exited)
        const currentSession = _termTarget ? _termTarget.split(':')[0] : '';
        if (_cmdOutputOpen && currentSession === session) {
            document.getElementById('cmd-output-dot').classList.add('stopped');
        }
    };

    state.ws.onerror = function () {
        // onclose fires after this
    };
}

function disconnectSplitWs(session) {
    const state = _splitPanes[session];
    if (!state) return;
    if (state.ws) {
        state.wsConnected = false;
        try { state.ws.close(); } catch (e) {}
        state.ws = null;
    }
}

// ================================================================
// Input target toggle — route keyboard between main and split pane
// ================================================================

function toggleInputTarget() {
    if (!_termTarget) return;
    const session = _termTarget.split(':')[0];
    const state = _splitPanes[session];
    if (!state) return;

    _inputToSplit = !_inputToSplit;
    updateInputToggleUI();
    updateTmuxIndicator();
}


function updateInputToggleUI() {
    const btn = document.getElementById('cmd-input-toggle');
    if (!btn) return;
    if (_inputToSplit) {
        btn.innerHTML = '&#9660;';  // down arrow = input going to bottom pane
        btn.classList.add('active');
        btn.title = 'Keyboard → split pane (tap to switch)';
    } else {
        btn.innerHTML = '&#9650;';  // up arrow = input going to top pane
        btn.classList.remove('active');
        btn.title = 'Keyboard → main pane (tap to switch)';
    }
}

// ================================================================
// Tab Switch — show/hide overlay when switching sessions
// ================================================================

function onTabSwitchCommands(newSession) {
    _currentCommandsProject = '';
    _currentCommands = [];
    _inputToSplit = false;
    updateInputToggleUI();

    const state = _splitPanes[newSession];
    if (state) {
        // Show overlay with cached content
        _cmdOutputOpen = true;
        const overlay = document.getElementById('cmd-output');
        const label = document.getElementById('cmd-output-label');
        const dot = document.getElementById('cmd-output-dot');
        const pre = document.getElementById('cmd-output-pre');

        label.textContent = state.label || 'Command';
        dot.classList.toggle('stopped', !state.wsConnected);
        pre.textContent = state.lastContent || '';
        overlay.classList.add('visible');

        // Reconnect WS if not connected
        if (!state.ws) connectSplitWs(newSession);
    } else {
        // No split pane for this session — hide overlay
        _cmdOutputOpen = false;
        document.getElementById('cmd-output').classList.remove('visible');
    }
}

// ================================================================
// Session Kill cleanup
// ================================================================

function onSessionKillCommands(session) {
    const state = _splitPanes[session];
    if (state) {
        disconnectSplitWs(session);
        delete _splitPanes[session];
    }
    if (_cmdOutputOpen) {
        _cmdOutputOpen = false;
        document.getElementById('cmd-output').classList.remove('visible');
    }
}

// ================================================================
// Orphan detection — check if split pane died externally
// ================================================================

async function checkSplitPaneAlive(session) {
    const state = _splitPanes[session];
    if (!state) return;
    try {
        const resp = await fetch(`/api/commands/pane/${encodeURIComponent(session)}`, {
            signal: AbortSignal.timeout(3000),
        });
        const data = await resp.json();
        if (!data.exists) {
            // Pane died — clean up
            disconnectSplitWs(session);
            delete _splitPanes[session];
            const currentSession = _termTarget ? _termTarget.split(':')[0] : '';
            if (_cmdOutputOpen && currentSession === session) {
                // Mark as stopped but keep output visible so user can read it
                document.getElementById('cmd-output-dot').classList.add('stopped');
            }
        }
    } catch (e) {}
}
