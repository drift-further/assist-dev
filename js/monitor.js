// monitor.js — Polling, health, session states, notifications, git actions

// -- Polling (WebSocket preferred, HTTP fallback) --
function startPolling() {
    stopPolling();
    if (_termTarget && _termOpen) {
        // Try WebSocket first — give it 2s to connect before falling back to HTTP
        connectTerminalWs();
        // Do one immediate HTTP capture for instant display
        captureTerminal();
        // Only start HTTP polling timer if WS fails to connect within 2s
        setTimeout(function() {
            if (!_termWsConnected && _termOpen && _termTarget && !_termPollTimer) {
                _termPollTimer = setInterval(captureTerminal, SETTINGS ? SETTINGS.connection.http_fallback_poll_ms : 3000);
                updateConnIndicator();
            }
        }, 2000);
    }
    // Session tab refresh is now handled by consolidatedPoll() every 5s
}

function stopPolling() {
    disconnectTerminalWs();
    if (_termPollTimer) {
        clearInterval(_termPollTimer);
        _termPollTimer = null;
    }
    updateConnIndicator();
}

// -- tmux indicator --
function _currentPaneDisplayName() {
    if (!_termTarget) return '';
    const pane = (_sessionPanes || []).find(p => p.target === _termTarget);
    if (pane && pane.agent_name) return agentDisplayName(pane);
    return shortName(_termTarget.split(':')[0]);
}

function updateTmuxIndicator() {
    if (_termTarget) {
        const name = _currentPaneDisplayName();
        const suffix = _inputToSplit ? ' (split)' : '';
        tmuxInd.textContent = '\u2192 ' + name + suffix;
        tmuxInd.classList.add('active');
    } else {
        tmuxInd.textContent = '';
        tmuxInd.classList.remove('active');
    }
    updateRouteIndicator();
}

function updateRouteIndicator() {
    if (_termTarget) {
        const name = _currentPaneDisplayName();
        const session = _termTarget.split(':')[0];
        routeDot.className = 'route-dot tmux';
        routeLabel.textContent = 'tmux \u2192 ' + name;
        routeLabel.style.color = 'var(--cyan)';
        routeAttach.textContent = '\u2192 tmux attach -t ' + session;
    } else {
        routeDot.className = 'route-dot desktop';
        routeLabel.textContent = 'Desktop (focused window)';
        routeLabel.style.color = 'var(--text-dim)';
        routeAttach.textContent = '';
    }
}

// ================================================================
// Health check + status time
// ================================================================
// checkHealth is now handled by consolidatedPoll() in app.js

function updateStatusTime() {
    if (!lastAction) {
        statusTime.textContent = '';
        return;
    }
    const secs = Math.floor((Date.now() - lastAction) / 1000);
    if (secs < 5) statusTime.textContent = 'just now';
    else if (secs < 60) statusTime.textContent = secs + 's ago';
    else if (secs < 3600) statusTime.textContent = Math.floor(secs / 60) + 'm ago';
    else statusTime.textContent = Math.floor(secs / 3600) + 'h ago';
}

// ================================================================
// Session state polling — now handled by consolidatedPoll() in app.js
// ================================================================

function sendDoneNotification(session) {
    if (_notifPermission !== 'granted') return;
    try {
        new Notification('Assist - ' + shortName(session), {
            body: 'Agent finished. Output ready.',
            tag: 'assist-done-' + session,
            renotify: true,
        });
    } catch(e) {}
}

// ================================================================
// Prompt notification scanner (all sessions)
// ================================================================
function requestNotifPermission() {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission === 'default') {
        Notification.requestPermission().then(p => { _notifPermission = p; });
    }
}

// scanAllSessionsForPrompts — now handled by consolidatedPoll() in app.js

function sendPromptNotification(session, detected) {
    if (_notifPermission !== 'granted') return;
    const shortSession = shortName(session);
    const body = detected.desc + ': ' + detected.actions.map(a => a.label).join(' / ');
    try {
        const n = new Notification('Assist \u2014 ' + shortSession, {
            body: body,
            icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y="80" font-size="80">\u26A1</text></svg>',
            tag: 'assist-prompt-' + session,
            renotify: true,
        });
        n.onclick = function() {
            window.focus();
            n.close();
        };
    } catch(e) {}
}

// ================================================================
// Git Quick Actions (isolated — runs in temp tmux session)
// ================================================================
function toggleGitPanel() {
    const panel = document.getElementById('git-panel');
    const btn = document.getElementById('btn-git');
    const visible = panel.classList.contains('visible');
    panel.classList.toggle('visible', !visible);
    btn.classList.toggle('active', !visible);
}

async function gitRunCommand(cmd) {
    showFlash('sent', 'Git: running...');
    try {
        const resp = await fetch('/api/git/run', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ command: cmd, target: getInputTarget() }),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', 'Git: done');
            lastAction = Date.now();
            updateStatusTime();
        } else {
            showFlash('error', data.error || 'Git failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

function gitStatus() { gitRunCommand('git status'); }
function gitPush() { gitRunCommand('git push'); }

async function createVenv() {
    showFlash('sent', 'Creating .venv...');
    try {
        const resp = await fetch('/api/venv/create', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ target: getInputTarget() }),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', '.venv created + activated');
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

function gitCommitPush() {
    const inp = document.getElementById('git-commit-msg');
    const msg = inp.value.trim();
    if (!msg) { showFlash('error', 'Enter a message'); return; }
    const escaped = msg.replace(/'/g, "'\\''");
    gitRunCommand("git add -A && git commit -m '" + escaped + "' && git push");
    inp.value = '';
}

// ================================================================
// Automate Run — launch claude-mount container
// ================================================================
function toggleAutoPanel() {
    const panel = document.getElementById('auto-panel');
    const btn = document.getElementById('btn-auto');
    const visible = panel.classList.contains('visible');
    panel.classList.toggle('visible', !visible);
    if (btn) btn.classList.toggle('active', !visible);
}

async function automateRun() {
    const promptEl = document.getElementById('auto-prompt');
    const timeoutEl = document.getElementById('auto-timeout');
    const contEl = document.getElementById('auto-continuous');
    const iterEl = document.getElementById('auto-iterations');
    const prompt = promptEl.value.trim();
    if (!prompt) { showFlash('error', 'Enter a prompt'); return; }

    const timeout = parseInt(timeoutEl.value) || 10;
    const continuous = contEl.checked;
    const iterations = continuous ? 0 : (parseInt(iterEl.value) || 1);
    const stopAfterEl = document.getElementById('auto-stop-after');
    const stop_after = stopAfterEl ? stopAfterEl.value : '';
    const claudeCmdEl = document.getElementById('auto-claude-cmd');
    const claude_cmd = claudeCmdEl ? claudeCmdEl.value : '';

    showFlash('sent', 'Launching automation...');
    try {
        const resp = await fetch('/api/automate/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ prompt, timeout, continuous, iterations, stop_after, claude_cmd }),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', 'Automation started');
            promptEl.value = '';
            _updateAutoUI({ active: true, status: 'running', session: data.session });
            // Auto-switch to the new session tab
            if (data.target) {
                _termTarget = data.target;
                updateTmuxIndicator();
                await loadSessions();
                await captureTerminal();
            }
        } else {
            showFlash('error', data.error || 'Failed to start');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

async function automateStop() {
    if (!confirm('Stop the running automation?')) return;
    showFlash('sent', 'Stopping...');
    try {
        const resp = await fetch('/api/automate/stop', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', 'Automation stopped');
            _updateAutoUI({ active: false, status: 'stopped' });
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

function _updateAutoUI(auto) {
    const runBtn = document.getElementById('auto-run-btn');
    const stopBtn = document.getElementById('auto-stop-btn');
    const statusEl = document.getElementById('auto-status');
    const contEl = document.getElementById('auto-continuous');
    const iterEl = document.getElementById('auto-iterations');
    const iterLabel = document.getElementById('auto-iter-label');
    if (!runBtn || !stopBtn || !statusEl) return;

    const stopAfterEl = document.getElementById('auto-stop-after');

    if (auto && auto.active) {
        runBtn.classList.add('hidden');
        stopBtn.classList.remove('hidden');
        statusEl.classList.remove('hidden');

        // Sync checkbox/iterations from server state
        if (contEl) contEl.checked = auto.continuous !== false;
        if (iterEl) {
            iterEl.disabled = auto.continuous !== false;
            if (!auto.continuous && auto.max_iterations > 0) iterEl.value = auto.max_iterations;
        }
        if (iterLabel) iterLabel.style.opacity = auto.continuous !== false ? '0.3' : '1';
        if (stopAfterEl && auto.stop_after) stopAfterEl.value = auto.stop_after;

        const elapsed = auto.elapsed_seconds || 0;
        const idle = auto.idle_seconds || 0;
        const timeoutMin = auto.timeout_minutes || 10;
        const em = Math.floor(elapsed / 60);
        const es = Math.floor(elapsed % 60);
        const im = Math.floor(idle / 60);
        const is_ = Math.floor(idle % 60);

        let iterText = '';
        if (auto.continuous === false && auto.max_iterations > 0) {
            iterText = ` · Run ${(auto.iterations_completed || 0) + 1}/${auto.max_iterations}`;
        } else if (auto.iterations_completed > 0) {
            iterText = ` · Runs: ${auto.iterations_completed}`;
        }

        let stopText = '';
        if (auto.stop_after) stopText = ` · Stop@${auto.stop_after}`;

        statusEl.className = 'auto-status';
        statusEl.textContent = `Running ${em}m${String(es).padStart(2,'0')}s · Idle ${im}m${String(is_).padStart(2,'0')}s / ${timeoutMin}m${iterText}${stopText}`;
    } else if (auto && auto.status) {
        runBtn.classList.remove('hidden');
        stopBtn.classList.add('hidden');
        statusEl.classList.remove('hidden');
        statusEl.className = 'auto-status ' + (auto.status || '');

        const labels = { completed: 'Completed', timed_out: 'Timed out', stopped: 'Stopped' };
        let label = labels[auto.status] || auto.status;
        if (auto.iterations_completed > 0) label += ` (${auto.iterations_completed} runs)`;
        statusEl.textContent = label;
    } else {
        runBtn.classList.remove('hidden');
        stopBtn.classList.add('hidden');
        statusEl.classList.add('hidden');
    }
}

function _initAutomateControls() {
    const contEl = document.getElementById('auto-continuous');
    const iterEl = document.getElementById('auto-iterations');
    const iterLabel = document.getElementById('auto-iter-label');
    if (!contEl) return;

    function syncIterState() {
        const checked = contEl.checked;
        if (iterEl) iterEl.disabled = checked;
        if (iterLabel) iterLabel.style.opacity = checked ? '0.3' : '1';
    }

    contEl.addEventListener('change', () => {
        syncIterState();
        // If automation is running, patch the server
        const stopBtn = document.getElementById('auto-stop-btn');
        if (stopBtn && !stopBtn.classList.contains('hidden')) {
            const iterations = contEl.checked ? 0 : (parseInt(iterEl.value) || 1);
            fetch('/api/automate/patch', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ continuous: contEl.checked, iterations }),
            });
        }
    });

    if (iterEl) {
        iterEl.addEventListener('change', () => {
            const stopBtn = document.getElementById('auto-stop-btn');
            if (stopBtn && !stopBtn.classList.contains('hidden') && !contEl.checked) {
                fetch('/api/automate/patch', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ iterations: parseInt(iterEl.value) || 1 }),
                });
            }
        });
    }

    syncIterState();

    const stopAfterEl = document.getElementById('auto-stop-after');
    if (stopAfterEl) {
        stopAfterEl.addEventListener('change', () => {
            const stopBtn = document.getElementById('auto-stop-btn');
            if (stopBtn && !stopBtn.classList.contains('hidden')) {
                // Live-patch running automation
                fetch('/api/automate/patch', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ stop_after: stopAfterEl.value }),
                });
            }
        });
    }
}

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    _initAutomateControls();
    _initProjectSettingsAutoSave();
    // Load project settings for initial session after a short delay (wait for _termTarget to be set)
    setTimeout(() => {
        if (_termTarget) loadProjectSettings(_termTarget.split(':')[0]);
    }, 1000);
});

// ================================================================
// Per-Project Automation Settings
// ================================================================

async function loadProjectSettings(project) {
    if (!project) return;
    _projectSettingsName = project;
    try {
        const resp = await fetch(`/api/project-settings/${encodeURIComponent(project)}`);
        const data = await resp.json();
        _projectSettings = data.ok ? data.settings : null;
    } catch (e) {
        _projectSettings = null;
    }
    // Pre-fill automate form from project defaults (only when automation is NOT running)
    const stopBtn = document.getElementById('auto-stop-btn');
    const isRunning = stopBtn && !stopBtn.classList.contains('hidden');
    if (_projectSettings && !isRunning) {
        const promptEl = document.getElementById('auto-prompt');
        const timeoutEl = document.getElementById('auto-timeout');
        const contEl = document.getElementById('auto-continuous');
        const iterEl = document.getElementById('auto-iterations');
        if (promptEl && !promptEl.value.trim()) promptEl.value = _projectSettings.automate.default_prompt || '';
        if (timeoutEl) timeoutEl.value = _projectSettings.automate.timeout;
        if (contEl) contEl.checked = _projectSettings.automate.continuous;
        if (iterEl) {
            iterEl.value = _projectSettings.automate.max_iterations || 1;
            iterEl.disabled = _projectSettings.automate.continuous;
        }
        const iterLabel = document.getElementById('auto-iter-label');
        if (iterLabel) iterLabel.style.opacity = _projectSettings.automate.continuous ? '0.3' : '1';
        const stopAfterEl = document.getElementById('auto-stop-after');
        if (stopAfterEl) stopAfterEl.value = _projectSettings.automate.stop_after || '';
    }
    // Update project name display
    const nameEl = document.getElementById('auto-proj-name');
    if (nameEl) nameEl.textContent = _projectSettingsName ? shortName(_projectSettingsName) : '';
    // Render settings panel
    renderProjectSettings();
    // Auto-enable auto-yes if project has enabled_default
    if (_projectSettings && _projectSettings.autoyes.enabled_default) {
        if (!isAutoYes(project)) {
            _enableAutoYes(project, _projectSettings.autoyes.delay);
        }
    }
}

function renderProjectSettings() {
    const body = document.getElementById('auto-proj-body');
    if (!body || !_projectSettings) { if (body) body.innerHTML = ''; return; }

    const s = _projectSettings;
    let html = '';

    // Auto-Yes section
    html += '<div class="proj-section-hdr">Auto-Yes</div>';
    html += _projRow('Delay', _projStepper('autoyes', 'delay', s.autoyes.delay, 1, 30, 's'));
    html += _projRow('Auto-enable', _projToggle('autoyes', 'enabled_default', s.autoyes.enabled_default));

    // Triggers section
    html += '<div class="proj-section-hdr">Triggers</div>';
    html += _projRow('Done signals', _projTextInput('triggers', 'done_signals', s.triggers.done_signals.join(', ')));
    html += _projRow('Done idle', _projStepper('triggers', 'done_idle_sec', s.triggers.done_idle_sec, 10, 300, 's'));
    html += _projRow('Trust auto-approve', _projToggle('triggers', 'trust_auto_approve', s.triggers.trust_auto_approve));
    html += _projRow('Relaunch wait', _projStepper('triggers', 'relaunch_wait_sec', s.triggers.relaunch_wait_sec, 5, 120, 's'));

    body.innerHTML = html;
}

function _projRow(label, control) {
    return `<div class="proj-row"><span class="proj-label">${label}</span>${control}</div>`;
}

function _projStepper(section, key, val, min, max, unit) {
    return `<div class="proj-stepper">
        <button class="proj-step-btn" onclick="projStep('${section}','${key}',-1,${min},${max})">-</button>
        <span class="proj-step-val" id="proj-${section}-${key}">${val}</span>
        <span class="proj-step-unit">${unit}</span>
        <button class="proj-step-btn" onclick="projStep('${section}','${key}',1,${min},${max})">+</button>
    </div>`;
}

function _projToggle(section, key, active) {
    const cls = active ? 'proj-tog active' : 'proj-tog';
    return `<button class="${cls}" id="proj-${section}-${key}" onclick="projToggle('${section}','${key}')">${active ? 'ON' : 'OFF'}</button>`;
}

function _projTextInput(section, key, val) {
    return `<input class="proj-text" id="proj-${section}-${key}" value="${escHtml(val)}"
        onblur="projTextSave('${section}','${key}',this.value)" placeholder="signal1, signal2">`;
}

async function _saveProjectSetting(section, key, value) {
    if (!_projectSettingsName) return;
    const patch = {};
    patch[section] = {};
    patch[section][key] = value;
    try {
        const resp = await fetch(`/api/project-settings/${encodeURIComponent(_projectSettingsName)}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(patch),
        });
        const data = await resp.json();
        if (data.ok) {
            _projectSettings = data.settings;
            showFlash('ok', 'Saved');
        }
    } catch (e) {
        showFlash('error', 'Save failed');
    }
}

function projStep(section, key, delta, min, max) {
    const el = document.getElementById(`proj-${section}-${key}`);
    if (!el) return;
    let val = parseInt(el.textContent) + delta;
    val = Math.max(min, Math.min(max, val));
    el.textContent = val;
    _saveProjectSetting(section, key, val);
}

function projToggle(section, key) {
    const el = document.getElementById(`proj-${section}-${key}`);
    if (!el) return;
    const active = !el.classList.contains('active');
    el.classList.toggle('active', active);
    el.textContent = active ? 'ON' : 'OFF';
    _saveProjectSetting(section, key, active);
}

function projTextSave(section, key, raw) {
    if (key === 'done_signals') {
        const arr = raw.split(',').map(s => s.trim()).filter(Boolean);
        _saveProjectSetting(section, key, arr);
    } else {
        _saveProjectSetting(section, key, raw);
    }
}

function toggleProjectSettings() {
    _projSettingsOpen = !_projSettingsOpen;
    const body = document.getElementById('auto-proj-body');
    const arrow = document.getElementById('auto-proj-arrow');
    if (body) body.classList.toggle('hidden', !_projSettingsOpen);
    if (arrow) arrow.classList.toggle('open', _projSettingsOpen);
}

function _initProjectSettingsAutoSave() {
    const timeoutEl = document.getElementById('auto-timeout');
    if (timeoutEl) {
        timeoutEl.addEventListener('change', () => {
            const stopBtn = document.getElementById('auto-stop-btn');
            if (stopBtn && stopBtn.classList.contains('hidden') && _projectSettingsName) {
                _saveProjectSetting('automate', 'timeout', parseInt(timeoutEl.value) || 10);
            }
        });
    }
    const promptEl = document.getElementById('auto-prompt');
    if (promptEl) {
        promptEl.addEventListener('blur', () => {
            const stopBtn = document.getElementById('auto-stop-btn');
            if (stopBtn && stopBtn.classList.contains('hidden') && _projectSettingsName) {
                _saveProjectSetting('automate', 'default_prompt', promptEl.value.trim());
            }
        });
    }
    const stopAfterEl = document.getElementById('auto-stop-after');
    if (stopAfterEl) {
        stopAfterEl.addEventListener('change', () => {
            const stopBtn = document.getElementById('auto-stop-btn');
            if (stopBtn && stopBtn.classList.contains('hidden') && _projectSettingsName) {
                _saveProjectSetting('automate', 'stop_after', stopAfterEl.value);
            }
        });
    }
}

// ================================================================
// Claude Info Bar
// ================================================================
function toggleClaudeInfo() {
    const bar = document.getElementById('claude-info-bar');
    const btn = document.getElementById('btn-info');
    const visible = bar.classList.contains('visible');
    bar.classList.toggle('visible', !visible);
    btn.classList.toggle('active', !visible);
}

function updateClaudeInfo(meta) {
    if (!meta || Object.keys(meta).length === 0) return;

    // Context usage
    const ctxEl = document.getElementById('ci-context');
    if (meta.used_percentage != null) {
        const pct = meta.used_percentage;
        const tokK = Math.floor((meta.tokens || 0) / 1000);
        const limK = Math.floor((meta.limit || 0) / 1000);
        const filled = Math.min(Math.max(Math.round(pct / 10), 0), 10);
        const bar = '\u2588'.repeat(filled) + '\u2591'.repeat(10 - filled);
        ctxEl.textContent = bar + ' ' + pct.toFixed(1) + '% (' + tokK + 'k/' + limK + 'k)';
        ctxEl.className = 'ci-context' + (pct >= 80 ? ' crit' : pct >= 50 ? ' warn' : '');
    } else {
        ctxEl.textContent = '';
    }

    // Duration
    const durEl = document.getElementById('ci-duration');
    if (meta.duration_ms) {
        const secs = meta.duration_ms / 1000;
        const h = Math.floor(secs / 3600);
        const m = Math.floor((secs % 3600) / 60);
        durEl.textContent = h > 0 ? h + 'h' + m + 'm' : m + 'm';
    } else {
        durEl.textContent = '';
    }

    // Cost
    const costEl = document.getElementById('ci-cost');
    if (meta.cost_usd) {
        costEl.textContent = '$' + meta.cost_usd.toFixed(2);
    } else {
        costEl.textContent = '';
    }

    // Block timer
    const blockEl = document.getElementById('ci-block');
    if (meta.block_active) {
        const elH = Math.floor((meta.block_elapsed || 0) / 3600);
        const elM = Math.floor(((meta.block_elapsed || 0) % 3600) / 60);
        const remH = Math.floor((meta.block_remaining || 0) / 3600);
        const remM = Math.floor(((meta.block_remaining || 0) % 3600) / 60);
        const elStr = elH > 0 ? elH + 'h' + elM + 'm' : elM + 'm';
        const remStr = remH > 0 ? remH + 'h' + remM + 'm' : remM + 'm';
        const pct = Math.min(100, Math.round((meta.block_elapsed / (5 * 3600)) * 100));
        const filled = Math.min(Math.max(Math.round(pct / 10), 0), 10);
        const bar = '\u2588'.repeat(filled) + '\u2591'.repeat(10 - filled);
        blockEl.textContent = bar + ' ' + elStr + '/' + remStr;
        blockEl.className = 'ci-block' + (pct >= 85 ? ' crit' : pct >= 60 ? ' warn' : '');
    } else {
        blockEl.textContent = '';
    }

    // Task
    const taskEl = document.getElementById('ci-task');
    taskEl.textContent = 'Task: ' + (meta.task || 'None');

    // Edited files
    const filesEl = document.getElementById('ci-files');
    filesEl.textContent = '\u270e ' + (meta.edited_files || 0);

    // Open tasks
    const tasksEl = document.getElementById('ci-tasks-count');
    tasksEl.textContent = '[' + (meta.open_tasks || 0) + ']';

    // Branch
    const branchEl = document.getElementById('ci-branch');
    branchEl.textContent = meta.branch ? '\u238b ' + meta.branch : '';

    // Model
    const modelEl = document.getElementById('ci-model');
    modelEl.textContent = meta.model || '';
}
