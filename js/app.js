// app.js — Init: DOMContentLoaded, event listeners, startup sequence

// Enter key → Send
input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!_sending) doPaste();
    }
});

// Poll for trailing newlines — safety net for mobile IMEs that insert \n on submit
// Only strips trailing newlines; internal newlines (from paste) are preserved
setInterval(function() {
    const val = input.value;
    if (val.endsWith('\n') || val.endsWith('\r')) {
        input.value = val.replace(/[\r\n]+$/, '');
        if (input.value.trim() && !_sending) doPaste();
    }
}, 150);

// Git commit input — Enter key submits
document.getElementById('git-commit-msg').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
        e.preventDefault();
        gitCommitPush();
    }
});

// ================================================================
// Consolidated polling — replaces 5 separate intervals with 2
// ================================================================

// 1s UI-only timer (no network)
setInterval(updateStatusTime, 1000);

// 5s consolidated server poll (health + sessions + states + scan)
async function consolidatedPoll() {
    try {
        const resp = await fetch('/poll', {signal: AbortSignal.timeout(8000)});
        const data = await resp.json();

        // Health
        dot.className = 'status-dot ' + (data.status === 'ok' ? 'ok' : 'err');
        if (data.tmux_target && !_termTarget) {
            _termTarget = data.tmux_target;
            updateTmuxIndicator();
            try { localStorage.setItem('term_target', _termTarget); } catch(e) {}
        }

        // Sessions — update tabs
        _applySessionsData(data.sessions || [], data.active_target || '');

        // States — update tab indicators
        _applyStatesData(data.states || {});

        // Scan — prompt detection + activity on background tabs
        _applyScanData(data.scan || []);

        // Status bar enrichment
        const sessionCount = (data.sessions || []).length;
        const promptCount = Object.keys(_sessionPrompts).filter(k => _sessionPrompts[k]).length;
        const statusTitle = document.querySelector('.status-title');
        if (statusTitle) {
            let text = 'Assist';
            if (sessionCount > 0) text += ' \u00B7 ' + sessionCount + ' session' + (sessionCount > 1 ? 's' : '');
            if (promptCount > 0) text += ' \u00B7 ' + promptCount + ' waiting';
            statusTitle.innerHTML = text + ' <span>// Claude Code</span>';
        }

        // Automate status
        if (typeof _updateAutoUI === 'function') {
            _updateAutoUI(data.automate || null);
        }

        // Claude info bar
        if (typeof updateClaudeInfo === 'function') {
            updateClaudeInfo(data.claude_meta || null);
        }

        // Orphaned split pane check
        for (const session of Object.keys(_splitPanes)) {
            checkSplitPaneAlive(session);
        }
    } catch (e) {
        dot.className = 'status-dot err';
    }
}

// Extract session tab rendering from loadSessions() into a data-driven function
function _applySessionsData(panes, activeTarget) {
    const container = document.getElementById('session-tabs');
    const current = _termTarget || activeTarget || '';

    _sessionPanes = panes;
    container.innerHTML = '';
    if (panes.length === 0) {
        container.innerHTML = '<span class="session-tabs-empty">No sessions</span>';
        return;
    }

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

        const tab = document.createElement('button');
        tab.className = 'session-tab';
        if (isAgent) tab.classList.add('agent-tab');
        if (isTeamLead) tab.classList.add('team-lead-tab');
        tab.dataset.target = p.target;
        if (p.agent_name) tab.dataset.agentName = p.agent_name;

        const label = aName || shortName(p.session);
        tab.textContent = label;

        if (isAgent && aColor) {
            tab.style.borderLeftWidth = '3px';
            tab.style.borderLeftColor = aColor;
        } else if (p.session.endsWith('-auto')) {
            tab.style.borderLeftWidth = '3px';
            tab.style.borderLeftColor = 'var(--amber)';
        }

        const dotEl = document.createElement('span');
        dotEl.className = 'tab-dot';
        tab.appendChild(dotEl);

        if (_sessionPrompts[p.target]) {
            tab.classList.add('has-prompt');
        }
        tab.onclick = function() { selectTab(p.target); };
        if (p.target === current) tab.classList.add('active');
        container.appendChild(tab);
        prevSession = p.session;
    }

    const hadTarget = !!_termTarget;
    if (current && panes.some(p => p.target === current)) {
        _termTarget = current;
    } else if (panes.length > 0 && !_termTarget) {
        _termTarget = panes[0].target;
    }

    if (_termTarget) {
        markActiveTab(_termTarget);
        updateTmuxIndicator();
        document.getElementById('term-display').classList.remove('hidden');
        // Hide projects on first session discovery only (not every poll)
        if (!hadTarget && _termShowProjects) {
            _termShowProjects = false;
            document.getElementById('term-projects').classList.add('hidden');
        }
    }

    // Hook: reorder tabs (pinned first, then saved order)
    if (typeof _postTabRender === 'function') _postTabRender();
}

function _formatIdleTime(seconds) {
    const min = Math.floor(seconds / 60);
    if (min < 60) return min + 'm';
    const h = Math.floor(min / 60);
    const m = min % 60;
    return m > 0 ? h + 'h' + m + 'm' : h + 'h';
}

function _applyStatesData(states) {
    const now = Date.now();
    for (const [target, info] of Object.entries(states)) {
        const prev = _sessionStates[target];
        const prevState = prev ? prev.state : null;
        let effectiveState = info.state;

        if (prevState === 'running' && effectiveState === 'shell') {
            effectiveState = 'done';
            _sessionStates[target] = { state: 'done', since: now, prevState: 'running' };
            const isActive = _termOpen && target === _termTarget;
            if (!isActive) sendDoneNotification(target.split(':')[0]);
        } else if (prev && prev.state === 'done' && (now - prev.since) > 60000) {
            effectiveState = info.state;
            _sessionStates[target] = { state: effectiveState, since: now, prevState: 'done' };
        } else if (prev && prev.state === 'done' && (now - prev.since) <= 60000) {
            effectiveState = 'done';
        } else {
            _sessionStates[target] = { state: effectiveState, since: now, prevState: prevState };
        }

        if (_sessionPrompts[target]) effectiveState = 'needs-input';

        const tab = document.querySelector(`.session-tab[data-target="${CSS.escape(target)}"]`);
        if (tab) {
            const hasContentActivity = !!_activityDecayTimers[target];
            tab.classList.remove('done', 'idle', 'has-prompt');
            if (!hasContentActivity) tab.classList.remove('running');
            if (hasContentActivity) {
                tab.classList.add('running');
            } else if (effectiveState === 'needs-input') {
                tab.classList.add('has-prompt');
            } else if (effectiveState !== 'shell') {
                tab.classList.add(effectiveState);
            }

            // Tab badges
            const existingBadge = tab.querySelector('.tab-badge');
            if (existingBadge) existingBadge.remove();

            if (effectiveState === 'needs-input') {
                const badge = document.createElement('span');
                badge.className = 'tab-badge tab-badge-prompt';
                badge.textContent = '?';
                tab.appendChild(badge);
            } else if (effectiveState === 'done') {
                const badge = document.createElement('span');
                badge.className = 'tab-badge tab-badge-done';
                badge.textContent = '\u2713';
                tab.appendChild(badge);
            }

            // --- Idle fade: graduated visual warning for idle sessions ---
            tab.classList.remove('idle-fade-1', 'idle-fade-2', 'idle-fade-3', 'idle-expired');
            const oldIdleTime = tab.querySelector('.tab-idle-time');
            if (oldIdleTime) oldIdleTime.remove();

            const isSessionIdle = !hasContentActivity &&
                effectiveState !== 'needs-input' &&
                effectiveState !== 'done';

            if (isSessionIdle && (now - _pageLoadedAt) > 12000) {
                const idleSec = info.idle_seconds || 0;
                const idleMin = idleSec / 60;
                if (idleMin >= 5) {
                    tab.classList.add(
                        idleMin >= 60 ? 'idle-expired' :
                        idleMin >= 15 ? 'idle-fade-3' :
                        idleMin >= 10 ? 'idle-fade-2' : 'idle-fade-1'
                    );
                    const idleBadge = document.createElement('span');
                    idleBadge.className = 'tab-idle-time';
                    idleBadge.textContent = _formatIdleTime(idleSec);
                    tab.appendChild(idleBadge);
                } else if (idleSec >= 30) {
                    // Countdown to 5-min idle threshold
                    const remaining = Math.max(0, 300 - Math.floor(idleSec));
                    const rm = Math.floor(remaining / 60);
                    const rs = remaining % 60;
                    const idleBadge = document.createElement('span');
                    idleBadge.className = 'tab-idle-time tab-idle-countdown';
                    idleBadge.textContent = rm + ':' + (rs < 10 ? '0' : '') + rs;
                    tab.appendChild(idleBadge);
                }
            }

            tab.dataset.idleSeconds = Math.floor(info.idle_seconds || 0);
        }
    }
}

function _applyScanData(scanPanes) {
    for (const pane of scanPanes) {
        const isActive = _termOpen && pane.target === _termTarget;
        if (isActive) continue;

        const detected = detectSmartActions(stripAnsi(pane.tail));
        const tab = document.querySelector(`.session-tab[data-target="${CSS.escape(pane.target)}"]`);

        if (detected) {
            if (!_sessionPrompts[pane.target]) {
                _sessionPrompts[pane.target] = true;
                if (tab) tab.classList.add('has-prompt');
                const tailHash = pane.tail.slice(-200);
                if (_notifSentFor[pane.target] !== tailHash) {
                    _notifSentFor[pane.target] = tailHash;
                    sendPromptNotification(pane.session, detected);
                }
            }
        } else {
            if (_sessionPrompts[pane.target]) {
                delete _sessionPrompts[pane.target];
                delete _notifSentFor[pane.target];
                if (tab) tab.classList.remove('has-prompt');
            }
        }

        const prev = _lastScanContent[pane.target];
        _lastScanContent[pane.target] = pane.tail;
        if (tab && !tab.classList.contains('active') && prev !== undefined && prev !== pane.tail && !detected) {
            tab.classList.add('running');
            if (_activityDecayTimers[pane.target]) clearTimeout(_activityDecayTimers[pane.target]);
            _activityDecayTimers[pane.target] = setTimeout(() => {
                tab.classList.remove('running');
                delete _activityDecayTimers[pane.target];
            }, 10000);
        }
    }
}

// ================================================================
// Startup
// ================================================================
function measureStatusBar() {
    document.documentElement.style.setProperty('--status-bar-h', document.querySelector('.status-bar').offsetHeight + 'px');
}
measureStatusBar();
document.fonts.ready.then(measureStatusBar);

// Initial data load
consolidatedPoll();
loadHistory();
initSudoButton();
initClipboardImagePaste();
requestNotifPermission();

// Two timers only: 1s UI clock + 5s server poll
setInterval(consolidatedPoll, SETTINGS ? SETTINGS.connection.poll_interval_ms : 5000);

// Restore tmux target from localStorage
try {
    const saved = localStorage.getItem('term_target');
    if (saved) _termTarget = saved;
} catch(e) {}
updateTmuxIndicator();
updateRouteIndicator();

// Terminal always on — start loading
loadProjects();
startPolling();
syncAutoYesState().then(() => { if (_termTarget) updateAutoYesUI(_termTarget.split(':')[0]); });
