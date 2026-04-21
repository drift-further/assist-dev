// settings.js — Settings panel rendering, inline editing, save, restart

let _settingsPanelOpen = false;

function toggleSettingsPanel() {
    const panel = document.getElementById('settings-panel');
    _settingsPanelOpen = !_settingsPanelOpen;
    panel.classList.toggle('visible', _settingsPanelOpen);
    if (_settingsPanelOpen) renderSettings();
}

// Section definitions: { key, label, fields: [{ key, label, type, suffix?, restart?, options?, min?, max?, step? }] }
const _SETTINGS_SECTIONS = [
    {
        key: 'server', label: 'Server Controls', fields: [
            { key: 'claude_mode', label: 'Claude Mode', type: 'toggle', options: ['npx', 'claude'] },
            { key: 'session_init_cmd', label: 'Session Init Cmd', type: 'text', restart: true },
            { key: 'projects_dir', label: 'Projects Dir', type: 'text', restart: true },
            { key: 'port', label: 'Port', type: 'number', restart: true, min: 1024, max: 65535 },
            { key: 'restart_cmd', label: 'Restart Command', type: 'text', restart: true },
        ]
    },
    {
        key: 'terminal', label: 'Terminal', fields: [
            { key: 'font_size', label: 'Font Size', type: 'stepper', min: 8, max: 24, suffix: 'px' },
            { key: 'default_cols', label: 'Default Cols', type: 'number', min: 40, max: 400 },
            { key: 'default_rows', label: 'Default Rows', type: 'number', min: 10, max: 200 },
            { key: 'capture_lines', label: 'Capture Lines', type: 'number', min: 100, max: 50000 },
            { key: 'tmux_history_limit', label: 'History Limit', type: 'number', min: 1000, max: 100000 },
            { key: 'idle_threshold_sec', label: 'Idle Threshold', type: 'number', min: 30, max: 3600, suffix: 's' },
        ]
    },
    {
        key: 'autoyes', label: 'Auto-Yes', fields: [
            { key: 'default_delay', label: 'Default Delay', type: 'stepper', min: 1, max: 30, suffix: 's' },
            { key: 'detection_depth', label: 'Detection Depth', type: 'number', min: 2, max: 30, suffix: ' lines' },
        ]
    },
    {
        key: 'connection', label: 'Connection', fields: [
            { key: 'poll_interval_ms', label: 'Poll Interval', type: 'number', min: 1000, max: 30000, suffix: 'ms' },
            { key: 'ws_heartbeat_sec', label: 'WS Heartbeat', type: 'number', min: 1, max: 30, suffix: 's' },
            { key: 'ws_reconnect_max_ms', label: 'WS Reconnect Max', type: 'number', min: 5000, max: 120000, suffix: 'ms' },
            { key: 'http_fallback_poll_ms', label: 'HTTP Fallback Poll', type: 'number', min: 1000, max: 30000, suffix: 'ms' },
        ]
    },
    {
        key: 'ui', label: 'UI & Behavior', fields: [
            { key: 'toast_duration_ms', label: 'Toast Duration', type: 'number', min: 2000, max: 30000, suffix: 'ms' },
            { key: 'max_toasts', label: 'Max Toasts', type: 'number', min: 1, max: 10 },
            { key: 'stale_tab_threshold_sec', label: 'Stale Tab Threshold', type: 'number', min: 300, max: 86400, suffix: 's' },
            { key: 'recent_projects_limit', label: 'Recent Projects', type: 'number', min: 5, max: 100 },
        ]
    },
    {
        key: 'limits', label: 'Limits', fields: [
            { key: 'max_history', label: 'Max History', type: 'number', min: 100, max: 50000 },
            { key: 'max_upload_mb', label: 'Max Upload', type: 'number', min: 1, max: 500, suffix: ' MB' },
            { key: 'max_capture_lines', label: 'Max Capture Lines', type: 'number', min: 1000, max: 100000 },
        ]
    },
];

function _formatUptime(sec) {
    if (sec < 60) return sec + 's';
    if (sec < 3600) return Math.floor(sec / 60) + 'm';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h + 'h' + (m ? m + 'm' : '');
}

function renderSettings() {
    if (!SETTINGS) return;

    // Update server info
    const detail = document.getElementById('settings-server-detail');
    detail.textContent = 'PID ' + (_serverPid || '?') + ' \u00b7 uptime ' + _formatUptime(_serverUptime);

    const body = document.getElementById('settings-body');
    body.innerHTML = '';

    for (const section of _SETTINGS_SECTIONS) {
        const hdr = document.createElement('div');
        hdr.className = 'settings-section-hdr';
        hdr.textContent = section.label;
        body.appendChild(hdr);

        const vals = SETTINGS[section.key] || {};
        for (const field of section.fields) {
            const row = document.createElement('div');
            row.className = 'settings-row';

            // Label
            const label = document.createElement('span');
            label.className = 'settings-label';
            label.innerHTML = field.label;
            if (field.restart) {
                label.innerHTML += ' <span class="settings-restart-tag">RESTART</span>';
            }
            row.appendChild(label);

            // Value control
            const val = vals[field.key];
            if (field.type === 'toggle') {
                row.appendChild(_renderToggle(section.key, field, val));
            } else if (field.type === 'stepper') {
                row.appendChild(_renderStepper(section.key, field, val));
            } else if (field.type === 'number') {
                row.appendChild(_renderTappableValue(section.key, field, val, 'number'));
            } else {
                row.appendChild(_renderTappableValue(section.key, field, val, 'text'));
            }

            body.appendChild(row);
        }
    }
}

function _renderToggle(sectionKey, field, current) {
    const wrap = document.createElement('div');
    wrap.className = 'settings-toggle';
    for (const opt of field.options) {
        const btn = document.createElement('button');
        btn.className = 'settings-toggle-opt' + (opt === current ? ' active' : '');
        btn.textContent = opt;
        btn.onclick = () => _saveSetting(sectionKey, field.key, opt);
        wrap.appendChild(btn);
    }
    return wrap;
}

function _renderStepper(sectionKey, field, current) {
    const wrap = document.createElement('div');
    wrap.className = 'settings-stepper';
    const minus = document.createElement('button');
    minus.className = 'settings-stepper-btn';
    minus.textContent = '\u2212';
    minus.onclick = () => {
        const next = Math.max(field.min || 0, current - (field.step || 1));
        _saveSetting(sectionKey, field.key, next);
    };
    const display = document.createElement('span');
    display.className = 'settings-value';
    display.textContent = current + (field.suffix || '');
    display.style.cursor = 'default';
    const plus = document.createElement('button');
    plus.className = 'settings-stepper-btn';
    plus.textContent = '+';
    plus.onclick = () => {
        const next = Math.min(field.max || 99999, current + (field.step || 1));
        _saveSetting(sectionKey, field.key, next);
    };
    wrap.appendChild(minus);
    wrap.appendChild(display);
    wrap.appendChild(plus);
    return wrap;
}

function _renderTappableValue(sectionKey, field, current, inputType) {
    const span = document.createElement('span');
    span.className = 'settings-value';
    const displayVal = current + (field.suffix || '');
    span.textContent = displayVal;
    span.onclick = () => _startInlineEdit(span, sectionKey, field, current, inputType);
    return span;
}

function _startInlineEdit(span, sectionKey, field, current, inputType) {
    const input = document.createElement('input');
    input.className = 'settings-input';
    input.type = inputType === 'number' ? 'number' : 'text';
    input.value = current;
    if (field.min !== undefined) input.min = field.min;
    if (field.max !== undefined) input.max = field.max;
    if (inputType === 'text') input.style.width = '140px';

    const parent = span.parentNode;
    parent.replaceChild(input, span);
    input.focus();
    input.select();

    const commit = () => {
        let val = inputType === 'number' ? Number(input.value) : input.value;
        if (inputType === 'number') {
            if (field.min !== undefined) val = Math.max(field.min, val);
            if (field.max !== undefined) val = Math.min(field.max, val);
            if (isNaN(val)) val = current;
        }
        _saveSetting(sectionKey, field.key, val);
    };

    input.onkeydown = (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        if (e.key === 'Escape') { e.preventDefault(); renderSettings(); }
    };
    input.onblur = commit;
}

async function _saveSetting(sectionKey, fieldKey, value) {
    const patch = {};
    patch[sectionKey] = {};
    patch[sectionKey][fieldKey] = value;
    try {
        const r = await fetch('/api/settings', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        const d = await r.json();
        if (d.ok) {
            SETTINGS = d.settings;
            // Update CLAUDE_CMD if mode changed
            if (sectionKey === 'server' && fieldKey === 'claude_mode') {
                const cmds = { npx: 'npx @anthropic-ai/claude-code', claude: 'claude' };
                CLAUDE_CMD = cmds[value] || cmds.npx;
            }
            // Apply font size immediately
            if (sectionKey === 'terminal' && fieldKey === 'font_size') {
                _termFontSize = value;
                document.getElementById('term-content').style.fontSize = value + 'px';
                try { localStorage.setItem('assist_font_size', value); } catch(e) {}
            }
            showFlash('ok', fieldKey.replace(/_/g, ' ') + ' updated');
        } else {
            showFlash('error', d.error || 'Save failed');
        }
    } catch(e) {
        showFlash('error', 'Offline');
    }
    if (_settingsPanelOpen) renderSettings();
}

async function restartServer() {
    if (!confirm('Restart Assist server?')) return;
    const overlay = document.getElementById('restart-overlay');
    overlay.classList.add('visible');

    try {
        await fetch('/api/restart', { method: 'POST' });
    } catch(e) {
        // Expected — server dies before response completes
    }

    // Poll health until server is back
    let attempts = 0;
    const poll = setInterval(async () => {
        attempts++;
        try {
            const r = await fetch('/health', { signal: AbortSignal.timeout(2000) });
            if (r.ok) {
                clearInterval(poll);
                // Reload settings from new server
                await loadSettings();
                overlay.classList.remove('visible');
                showFlash('ok', 'Server restarted');
                if (_settingsPanelOpen) renderSettings();
            }
        } catch(e) {
            // Still restarting
        }
        if (attempts > 30) {
            clearInterval(poll);
            overlay.classList.remove('visible');
            showFlash('error', 'Restart timed out — check server');
        }
    }, 1000);
}

async function resetAllSettings() {
    if (!confirm('Reset all settings to defaults? Server restart may be required.')) return;
    try {
        // Fetch current state to get defaults
        const defResp = await fetch('/api/settings');
        if (defResp.ok) {
            const d = await defResp.json();
            const defaults = d.defaults;
            const resetResp = await fetch('/api/settings', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(defaults),
            });
            const resetData = await resetResp.json();
            if (resetData.ok) {
                SETTINGS = resetData.settings;
                showFlash('ok', 'Settings reset to defaults');
                renderSettings();
            }
        }
    } catch(e) {
        showFlash('error', 'Reset failed');
    }
}
