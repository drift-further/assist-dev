// app.js — Init: DOMContentLoaded, event listeners, startup sequence

// Empty composer: physical arrow keys control the terminal.
const terminalArrowKeys = {
    ArrowUp: 'Up',
    ArrowDown: 'Down',
    ArrowLeft: 'Left',
    ArrowRight: 'Right',
};

input.addEventListener('keydown', function(e) {
    const arrowKey = terminalArrowKeys[e.key];
    const hasModifier = e.altKey || e.ctrlKey || e.metaKey || e.shiftKey;
    if (!e.defaultPrevented && !e.isComposing && !_sending && !input.value &&
        !hasModifier && arrowKey) {
        e.preventDefault();
        sendKey(arrowKey);
        return;
    }

    // Enter key → Send, unless this tab's Enter lock is disarmed (spec §4.1),
    // in which case the keypress falls through untouched and inserts a newline.
    if (e.key === 'Enter' && !e.shiftKey) {
        if (typeof draftEnterArmed === 'function' && !draftEnterArmed()) return;
        e.preventDefault();
        if (!_sending) doPaste();
    }
});

// Poll for trailing newlines — safety net for mobile IMEs that insert \n on submit
// Only strips trailing newlines; internal newlines (from paste) are preserved
setInterval(function() {
    // Disarmed Enter lock: FULLY inert for this target (spec §4.2). Not "strip
    // but don't send" — the moment Enter means newline, a trailing newline is
    // legitimate content, and stripping it would silently delete what was just
    // typed. There is no Shift+Enter on a phone keyboard, so this timer, not
    // the keydown above, is what makes a multi-line prompt possible at all.
    if (typeof draftEnterArmed === 'function' && !draftEnterArmed()) return;
    const val = input.value;
    if (val.endsWith('\n') || val.endsWith('\r')) {
        input.value = val.replace(/[\r\n]+$/, '');
        // Autocomplete open: a mobile "send" keystroke accepts the highlighted
        // suggestion instead of submitting the message.
        if (typeof acConsumeEnter === 'function' && acConsumeEnter()) return;
        if (input.value.trim() && !_sending) doPaste();
    }
}, 150);

// Git commit input — Enter never submits. The button stages everything, commits
// and pushes, so it runs only from an explicit tap and a confirm that names the
// branch, the remote and how many files go with it. Enter just closes the keyboard.
document.getElementById('git-commit-msg').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
        e.preventDefault();
        e.target.blur();
    }
});

// The pane and message are captured once, before the preview, and exactly those
// are dispatched: the preview is async, and a tab switch while it is pending
// used to confirm one repository and then commit and push the other.
async function gitCommitPushConfirm() {
    const box = document.getElementById('git-commit-msg');
    const msg = box.value.trim();
    const target = getInputTarget();
    if (!msg) { showFlash('error', 'Enter a message'); return; }
    if (!target) { showFlash('error', 'No active session'); return; }
    let info;
    try {
        const resp = await fetch('/api/git/preview?target=' + encodeURIComponent(target));
        if (authLost(resp)) return;
        info = await resp.json();
    } catch (e) {
        showFlash('error', 'Offline');
        return;
    }
    if (!info.ok) { showFlash('error', info.error || 'Git preview failed'); return; }
    if (getInputTarget() !== target) {
        showFlash('error', 'Session changed — tap Commit & push again');
        return;
    }
    const files = info.changed + ' changed file' + (info.changed === 1 ? '' : 's');
    const dest = info.upstream
        ? info.upstream + (info.remote_url ? ' (' + info.remote_url + ')' : '')
        : 'no upstream set, so the push will fail';
    const text = 'Commit & push?\n\n'
        + 'Pane: ' + target + (info.dir ? ' (' + info.dir + ')' : '') + '\n'
        + 'Branch: ' + info.branch + '\n'
        + 'Push to: ' + dest + '\n'
        + files + ', all staged with git add -A\n\n'
        + 'Message: ' + msg;
    if (!confirm(text)) return;
    gitRunOp('commit_push', msg, target, info.dir);
    // Clear only the message that was sent; a newer draft typed meanwhile stays.
    if (box.value.trim() === msg) box.value = '';
}

// The ◇ button is the whole doorway: connected, it deep-links the active
// pane's project; anything else, it opens the connect sheet. The server
// resolves cwd -> Studio project; on any failure we still open Studio home so
// the button never dead-ends.
async function openStudio() {
    if (typeof _studioState !== 'undefined' && _studioState !== 'connected') {
        if (typeof openStudioConnect === 'function') { openStudioConnect(); return; }
    }
    let url = null, name = null;
    try {
        const target = (typeof _termTarget !== 'undefined' && _termTarget) ? _termTarget : '';
        const resp = await fetch('/studio/link?target=' + encodeURIComponent(target));
        const data = await resp.json();
        url = data && data.url;
        name = data && data.project_name;
    } catch (e) {}
    if (!url) {
        const base = (typeof _studioWebBase !== 'undefined' && _studioWebBase) ? _studioWebBase : '';
        if (!base) { if (typeof openStudioConnect === 'function') openStudioConnect(); return; }
        url = base.replace(/\/$/, '') + '/#/';
    }
    if (typeof showFlash === 'function') showFlash('ok', name ? ('Studio → ' + name) : 'Studio');
    window.open(url, '_blank', 'noopener');
}

// ================================================================
// Consolidated polling — replaces 5 separate intervals with 2
// ================================================================

// 1s UI-only timer (no network)
setInterval(updateStatusTime, 1000);

// 5s consolidated server poll (health + sessions + states + scan)
async function consolidatedPoll() {
    // Taken before the request: a list the server built before a pane this
    // browser just created existed must not move us off it (js/target-hold.js).
    const requestedAt = Date.now();
    const req = pollBegin();
    try {
        const url = req.since ? '/poll?since=' + encodeURIComponent(req.since) : '/poll';
        const resp = await fetch(url, {signal: AbortSignal.timeout(8000)});
        if (authLost(resp)) return;
        const data = await resp.json();
        // Overlapping polls (js/poll-sync.js): an older response is dropped,
        // and tails the server left out are filled from the ones held here.
        if (!pollAccept(req.seq)) return;
        if (data.scan) pollMergeScan(data.gen, data.scan);

        // Health
        dot.className = 'status-dot ' + (data.status === 'ok' ? 'ok' : 'err');
        if (data.tmux_target && !_termTarget) {
            _termTarget = data.tmux_target;
            updateTmuxIndicator();
            try { localStorage.setItem('term_target', _termTarget); } catch(e) {}
        }

        // Tab order/pin/snooze — must land before the tabs are rebuilt, since
        // the session list arrives already sorted by it.
        if (typeof _applyTabState === 'function') _applyTabState(data.tab_state);

        // Sessions — update tabs
        _applySessionsData(data.sessions || [], data.active_target || '', requestedAt);

        // States — update tab indicators
        _applyStatesData(data.states || {});

        // The server's detection depth, before the scan is detected against it
        if (Number.isInteger(data.detection_depth)) _detectionDepth = data.detection_depth;

        // Scan — prompt detection + activity on background tabs
        _applyScanData(data.scan || []);

        // A sudo prompt in any pane — a Yes/No popup (prompt-popup.js)
        if (typeof _applySudoPrompts === 'function') _applySudoPrompts(data.sudo_prompts || []);

        // Composer drafts — tab markers, plus the resync that lets a draft
        // written on one device turn up on the other. Runs after the strip is
        // rebuilt so the markers land on the fresh nodes.
        if (typeof _applyDraftsData === 'function') _applyDraftsData(data.drafts || null);

        // Status bar enrichment
        const sessionCount = (data.sessions || []).length;
        const promptCount = Object.keys(_sessionPrompts).filter(k => _sessionPrompts[k]).length;
        const statusTitle = document.querySelector('.status-title');
        if (statusTitle) {
            // The brand lockup beside it names the app; this is the counts.
            const parts = [];
            if (sessionCount > 0) parts.push(sessionCount + ' session' + (sessionCount > 1 ? 's' : ''));
            if (promptCount > 0) parts.push(promptCount + ' waiting');
            statusTitle.textContent = parts.join(' \u00B7 ');
        }

        // Automate status
        if (typeof _updateAutoUI === 'function') {
            _updateAutoUI(data.automate || null);
        }

        // Claude info bar
        if (typeof updateClaudeInfo === 'function') {
            updateClaudeInfo(data.claude_meta || null);
        }

        // Studio attention — snapshot from the server refresher, no Studio I/O
        if (typeof _applyStudioData === 'function') {
            _applyStudioData(data.studio || null);
        }

        // Open-access window — authoritative countdown; the strip interpolates
        // locally between polls.
        if (typeof _applyAccessData === 'function') {
            _applyAccessData(data.access || null);
        }

        // Collapsed-chrome grabber mirrors #status-dot / #studio-badge.
        if (typeof syncChromeGrabber === 'function') syncChromeGrabber();

        // Orphaned split pane check
        for (const session of Object.keys(_splitPanes)) {
            checkSplitPaneAlive(session);
        }
    } catch (e) {
        dot.className = 'status-dot err';
        // An aborted or failed poll leaves the tab strip as it was, which on a
        // fresh load is empty — the app then looks sessionless until something
        // else renders tabs. /terminal/sessions is the cheap path that never
        // does per-pane work: the strip may be stale, never blank.
        if (typeof loadSessions === 'function' &&
            !document.querySelector('#session-tabs .session-tab')) {
            loadSessions();
        }
    }
}

// Per-browser record of which model changes this user has actually looked at.
// The server's model_changed_at says WHEN it changed; this says whether we care.
const _MODEL_SEEN_KEY = 'assist.modelSeen';

function _modelSeenRead() {
    try { return JSON.parse(localStorage.getItem(_MODEL_SEEN_KEY)) || {}; }
    catch (e) { return {}; }
}

function _modelSeenWrite(seen) {
    try { localStorage.setItem(_MODEL_SEEN_KEY, JSON.stringify(seen)); } catch (e) {}
}

// Clear a tab's caret: we have now looked at it. Writes localStorage AND
// strips the class from the live DOM node immediately (S1) — without the DOM
// write, the caret lingers until the next strip rebuild (~5s), or
// indefinitely while polling is failing, because nothing else re-renders it
// between now and then. Uses the same CSS.escape(target) idiom
// markActiveTab() already uses for its `.session-tab[data-target="…"]`
// selector, for the same reason: `target` can contain characters CSS.escape
// must neutralize.
function _markModelSeen(target) {
    const pane = (_sessionPanes || []).find(p => p.target === target);
    if (!pane) return;
    const seen = _modelSeenRead();
    const changed = pane.model_changed_at || 0;
    // Never move a seen value backwards (S3) — see _modelSeenSweep() below
    // for why an overlapping renderer can otherwise hand this a stale value.
    if (changed > (seen[target] || 0)) seen[target] = changed;
    _modelSeenWrite(seen);
    const line = document.querySelector(
        `.session-tab[data-target="${CSS.escape(target)}"] .tab-model`
    );
    if (line) line.classList.remove('changed');
}

// Runs once per render, after the strip is rebuilt: clears the active tab's
// caret (a switch you watched happen should not leave one on the tab you are
// looking at) and prunes targets that no longer exist so the key cannot grow
// without bound.
//
// The `seen` write is monotonic (S3): _applySessionsData() (poll-driven) and
// loadSessions() (/terminal/sessions — launch/kill-driven) both call this
// and can be in flight at once. If a delayed loadSessions() response
// carrying an OLDER model_changed_at were allowed to lower a `seen` entry a
// newer render already raised, the next poll would see its own
// model_changed_at as newer than the (now-lowered) seen value again and
// resurrect a caret for a change already viewed. Also strips the caret from
// the live DOM node immediately for the active target, same reasoning as
// _markModelSeen() (S1) — a render-driven sweep is not a click, so nothing
// else would clear it before the next full rebuild otherwise.
function _modelSeenSweep(panes, activeTarget) {
    const live = new Set(panes.map(p => p.target));
    const seen = _modelSeenRead();
    let dirty = false;
    for (const key of Object.keys(seen)) {
        if (!live.has(key)) { delete seen[key]; dirty = true; }
    }
    for (const key of Object.keys(_paneModelLast)) {
        if (!live.has(key)) delete _paneModelLast[key];
    }
    if (activeTarget && live.has(activeTarget)) {
        const pane = panes.find(p => p.target === activeTarget);
        const changed = (pane && pane.model_changed_at) || 0;
        if (changed > (seen[activeTarget] || 0)) {
            seen[activeTarget] = changed;
            dirty = true;
        }
        const line = document.querySelector(
            `.session-tab[data-target="${CSS.escape(activeTarget)}"] .tab-model`
        );
        if (line) line.classList.remove('changed');
    }
    if (dirty) _modelSeenWrite(seen);
}

// Shared by both tab strips — _applySessionsData() here and loadSessions() in
// terminal.js. The strip is rebuilt from scratch every poll, so this runs on a
// fresh element each time and must not assume prior DOM state.
function applyTabModel(tab, pane) {
    const model = pane.model || '';
    const effort = pane.model_effort || '';
    const text = model ? (effort ? model + '·' + effort : model) : '';
    let line = tab.querySelector('.tab-model');
    if (!line) {
        line = document.createElement('span');
        line.className = 'tab-model';
        tab.appendChild(line);
    }
    line.textContent = text;
    if (!text) return;

    const target = pane.target;
    const changed = pane.model_changed_at || 0;

    // Pulse once, on the render where the value actually moved. The
    // already-defined guard suppresses a pulse on first sight after a page
    // load — otherwise every reload would strobe the whole strip.
    //
    // _paneModelLast is monotonic (S3): _applySessionsData() (poll-driven)
    // and loadSessions() (/terminal/sessions — launch/kill-driven) both call
    // this and can be in flight at once. A delayed loadSessions() response
    // carrying an OLDER model_changed_at must not overwrite a newer value a
    // poll already recorded — that would kill an already-fired pulse and let
    // the NEXT poll see `changed > prev` again and pulse a second time for a
    // change already shown. Math.max refuses to move the value backwards no
    // matter which renderer runs last.
    const prev = _paneModelLast[target];
    if (prev !== undefined && changed > prev) line.classList.add('pulse');
    _paneModelLast[target] = Math.max(prev === undefined ? 0 : prev, changed);

    // Caret persists until this browser opens the tab. An absent seen entry
    // counts as 0, so a switch missed while the page was closed still shows.
    // (The write side of this same monotonic rule — never LOWERING a stored
    // seen value — lives in _markModelSeen() and _modelSeenSweep() above,
    // S3, since this function only reads `seen`.)
    const seen = _modelSeenRead()[target] || 0;
    if (changed > seen) line.classList.add('changed');
}

// Extract session tab rendering from loadSessions() into a data-driven function
function _applySessionsData(panes, activeTarget, requestedAt) {
    // Skip the rebuild while a tab drag or reorder placement is in progress —
    // innerHTML='' mid-interaction reorders the wrong tab and persists the
    // corrupted order to localStorage.
    if (typeof _tabsInteractionActive === 'function' && _tabsInteractionActive()) return;
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

    for (const p of panes) {
        const isAgent = !!p.agent_name;
        // Mirrors loadSessions() in terminal.js — keep the two in sync.
        const isSubpane = !isAgent && !!p.is_subpane;
        const isTeamLead = !isAgent && teamSessions.has(p.session);
        const aName = agentDisplayName(p);
        const aColor = _agentColors[p.agent_color] || '';

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

        // Mirrors loadSessions() in terminal.js — keep the two in sync.
        applyTabModel(tab, p);

        if (_sessionPrompts[p.target]) {
            tab.classList.add('has-prompt');
        }
        tab.onclick = function() { selectTab(p.target); };
        if (p.target === current) tab.classList.add('active');
        container.appendChild(tab);
    }

    const hadTarget = !!_termTarget;
    if (current && panes.some(p => p.target === current)) {
        _termTarget = current;
        noteTargetListed(current);
    } else if (current && panes.length > 0 && targetMissingIsStale(current, requestedAt || 0)) {
        // Missing only because this list predates a pane we just created — keep
        // it. Mirrors loadSessions() in terminal.js — keep the two in sync.
        _termTarget = current;
    } else if (current && panes.length > 0) {
        // Viewed pane vanished (e.g. a subagent pane exited): fall back to
        // that session's first surviving pane rather than a dead target.
        // Mirrors loadSessions() in terminal.js — keep the two in sync.
        const sess = current.split(':')[0];
        const fallback = panes.find(p => p.session === sess) || panes[0];
        selectTab(fallback.target, true);   // automatic, not a tap — see selectTab
        // selectTab covers markActiveTab/indicator/unhide; still run the
        // reorder hook on the freshly rebuilt strip before bailing out.
        if (typeof _postTabRender === 'function') _postTabRender();
        return;
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

    _modelSeenSweep(panes, _termTarget);

    // The strip was rebuilt from scratch, so the draft markers went with it.
    // Mirrors loadSessions() in terminal.js — keep the two in sync.
    if (typeof _renderDraftMarks === 'function') _renderDraftMarks();

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
    // Re-evaluate stale group now that idleSeconds is populated.
    if (typeof _applyStaleGroup === 'function') _applyStaleGroup();
}

function _applyScanData(scanPanes) {
    // For the question popup (prompt-popup.js): prompts detected in tabs not in
    // view, plus the tab in view, whose prompt counts as seen.
    const questions = [];
    for (const pane of scanPanes) {
        const isActive = _termOpen && pane.target === _termTarget;
        if (isActive) {
            if (pane.prompt) {
                questions.push({target: pane.target, session: pane.session, prompt: pane.prompt, inView: true});
            }
            continue;
        }

        const raw = detectSmartActions(
            detectionTail(pane.tail),
            pane.target,
            pane.agent_kind
        );
        // A passive detection is an OFFER, not a question — nothing is waiting
        // on the human, so it must not badge the tab or fire a notification.
        // Without this, Claude Code's "claude --resume <uuid>" farewell keeps a
        // dead pane's tab marked as needing input forever.
        const detected = (raw && raw.passive) ? null : raw;
        const tab = document.querySelector(`.session-tab[data-target="${CSS.escape(pane.target)}"]`);
        if (detected && pane.prompt) {
            questions.push({target: pane.target, session: pane.session, detected, prompt: pane.prompt});
        }

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
            const wasRunning = tab.classList.contains('running');
            tab.classList.add('running');
            if (!wasRunning) {
                // Waking a snoozed tab is the server's call now (see
                // shared/tab_state.py: sweep_wakes) — this edge can't see it.
                // The `running` class latches while a pane keeps producing
                // output, so a tab snoozed mid-run never hits this branch at
                // all, which is exactly why the old client-side wake failed.
                if (typeof _applyStaleGroup === 'function') _applyStaleGroup();
            }
            if (_activityDecayTimers[pane.target]) clearTimeout(_activityDecayTimers[pane.target]);
            _activityDecayTimers[pane.target] = setTimeout(() => {
                tab.classList.remove('running');
                delete _activityDecayTimers[pane.target];
                if (typeof _applyStaleGroup === 'function') _applyStaleGroup();
            }, 10000);
        }
    }
    if (typeof _applyQuestionPrompts === 'function') _applyQuestionPrompts(questions);
}

// ================================================================
// Startup
// ================================================================
function measureStatusBar() {
    // .status-bar generates no box while the wrapping rail is on
    // (display: contents), so offsetHeight reads 0 and the left drawer plus the
    // notification stack would ride up to y=0 over the tabs. There the whole
    // chrome IS the status bar, so measure that instead. Keyed on the computed
    // display rather than on a zero height, because zero is also what the
    // collapsed default chrome reports — and that 0 is the value it has always
    // published.
    const bar = document.querySelector('.status-bar');
    let h = bar ? bar.offsetHeight : 0;
    if (bar && getComputedStyle(bar).display === 'contents') {
        const chrome = document.querySelector('.top-chrome');
        h = chrome ? chrome.offsetHeight : 0;
    }
    document.documentElement.style.setProperty('--status-bar-h', h + 'px');
}
measureStatusBar();
document.fonts.ready.then(measureStatusBar);

// Initial data load
consolidatedPoll();
loadHistory();
initClipboardImagePaste();
requestNotifPermission();

// Two timers only: 1s UI clock + self-rescheduling server poll.
// A setTimeout loop (not setInterval) so connection.poll_interval_ms is
// re-read on every tick — the async settings fetch hasn't resolved yet
// when this first runs.
//
// A hidden page skips its polls, unless it is how system notifications for
// prompts reach a background desktop tab (sendPromptNotification). It polls
// once the moment it is shown again.
function _pollWhileHidden() {
    return _notifPermission === 'granted';
}

function _schedulePoll() {
    const interval = SETTINGS?.connection?.poll_interval_ms || 5000;
    setTimeout(async () => {
        if (!document.hidden || _pollWhileHidden()) await consolidatedPoll();
        _schedulePoll();
    }, interval);
}
_schedulePoll();

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) consolidatedPoll();
});

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
