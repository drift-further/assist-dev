// js/keylabels.js — agent-aware sub-labels for the two keystroke surfaces.
//
// The buttons themselves never move and never change what they send: same key,
// same position, same handler. Only the small sub-label under each one is
// rewritten, because four of the seven keys both surfaces expose mean different
// things across the three CLIs, and two of them differ in how destructive they
// are. Adding a Codex button to a shared panel
// would make the Claude case worse, so the panel becomes agent-aware instead.
//
// Every override below was confirmed against the CLI running on this host —
// codex-cli v0.147.0 — by sending the key and reading the pane back. Anything
// the audit asserted but that did not reproduce is deliberately absent: the
// button then keeps its base label rather than carrying a claim we cannot show.

// What index.html ships. This is Claude Code's vocabulary and stays the default
// for claude, shell, unknown, and any kind we have not characterised.
const KEY_LABELS_BASE = {
    'Escape': 'dismiss',
    'Escape Escape': 'rewind',
    'ctrl+c': 'cancel',
    'ctrl+c ctrl+c': 'exit',
    'Return': 'submit',
    'ctrl+l': 'clear',
    'ctrl+r': 'search',
    'Tab': 'complete',
    'ctrl+d': 'exit',
    'ctrl+u': 'del line',
    'ctrl+a': 'home',
    'ctrl+e': 'end',
    'ctrl+k': 'del fwd',
    'shift+Tab': 'perm mode',
    'ctrl+o': 'verbose',
    'ctrl+t': 'tasks',
    'ctrl+g': 'editor',

    // A null base entry expresses an agent-exclusive button: it stays hidden
    // until that agent's override table claims it.
    'alt+period': null,
    'alt+comma': null,
    'alt+r': null,
    'ctrl+y': null,
    'ctrl+slash': null,
};

// null hides the button outright.
const KEY_LABELS_BY_AGENT = {
    codex: {
        // Esc is Codex's Interrupt Turn — its own working indicator says
        // "esc to interrupt". Labelling it "dismiss" pointed users away from
        // the actual stop key.
        'Escape': 'interrupt',
        'Escape Escape': 'edit prev',

        // Ctrl+C is context-dependent: it interrupts mid-turn, clears the
        // composer when idle with a draft, and EXITS IMMEDIATELY — no
        // confirmation — when idle with an empty composer. The label names the
        // outcome you cannot undo, because that is the one a mis-tap costs you.
        // Esc above covers the interrupt case, so nothing is lost by it.
        'ctrl+c': 'exit',
        // Redundant on Codex: the first press has already exited.
        'ctrl+c ctrl+c': null,

        // Verified both halves: Tab submits an idle composer, and queues a
        // follow-up while a turn is running.
        'Tab': 'submit/queue',
        'ctrl+r': 'history',
        // Readline delete-forward, NOT exit — verified ABCDEF -> BCDEF with the
        // session still alive.
        'ctrl+d': 'del char',
        'shift+Tab': 'approval mode',
        'ctrl+t': 'transcript',
        // Copies to the clipboard of the machine Assist runs on, not the phone's.
        'ctrl+o': 'copy (host)',
        'alt+period': 'reason +',
        'alt+comma': 'reason -',
        'alt+r': 'raw',
    },
    cursor: {
        // Deliberately sparse. cursor-agent v2026.08.04 did not reproduce the
        // audit's Ctrl+R "request review" or Ctrl+B x2 "background" bindings:
        // both were measured and did nothing. Ctrl+O and Ctrl+T likewise keep
        // their base labels.
        //
        // Two more Cursor claims were measured and refuted, which
        // is why index.html's prefix row gives Cursor only ! and /:
        //   '@' file paths   — echoes as plain text, no picker. Re-tested with
        //                      README.md and RELEASE.txt in the workspace and
        //                      with a search term ('@RE'), in case an empty dir
        //                      was starving it. Still nothing. Codex's '@RE' in
        //                      the same shape opens a live filtered picker.
        //   '&' cloud agent  — echoes as plain text, no affordance.
        'shift+Tab': 'agent/plan/ask',
        'ctrl+y': 'resume',
        'ctrl+slash': 'model',
    },
};

// Kinds absent here hide the group rather than borrowing another CLI's claims.
const COMMAND_GROUPS_BY_AGENT = {
    claude: {
        label: 'Claude Code',
        commands: ['/compact', '/clear', '/help', '/context', '/cost', '/status', '/model', '/config'],
    },
    codex: {
        label: 'Codex',
        commands: ['/model', '/review', '/diff', '/status', '/compact', '/plan', '/skills', '/permissions'],
    },
    cursor: {
        label: 'Cursor',
        // Do not add /opus-5, /gpt-5-6-sol, /composer-2-5,
        // /cursor-grok-4-5, or /opus-4-8 here. Cursor generates those shortcuts
        // from the signed-in account's access, so they go stale; /model does not.
        commands: ['/model', '/plan', '/ask', '/resume', '/summarize', '/clear', '/fork', '/rewind'],
    },
};

function keyLabelsFor(agentKind) {
    const overrides = KEY_LABELS_BY_AGENT[agentKind] || {};
    return Object.assign({}, KEY_LABELS_BASE, overrides);
}

// Resolve the agent kind for a target the same way smart actions do, preferring
// the WebSocket frame cache and falling back to the poll's pane list, which is
// what exists before the first frame arrives on a freshly opened tab.
function _agentKindFor(target) {
    const info = (typeof _paneInfo !== 'undefined') ? _paneInfo[target] : null;
    if (info && info.agent_kind) return info.agent_kind;
    const panes = (typeof _sessionPanes !== 'undefined' && _sessionPanes) || [];
    const pane = panes.find(p => p.target === target);
    return (pane && pane.agent_kind) || 'unknown';
}

function renderCommandGroup(agentKind) {
    const label = document.querySelector('#hk-cmd-label');
    const grid = document.querySelector('#hk-cmd-grid');
    if (!label || !grid) return;

    const group = Object.prototype.hasOwnProperty.call(COMMAND_GROUPS_BY_AGENT, agentKind)
        ? COMMAND_GROUPS_BY_AGENT[agentKind]
        : null;
    label.classList.toggle('hk-hidden', !group);
    grid.classList.toggle('hk-hidden', !group);
    if (!group) return;

    if (grid.dataset.agentKind === agentKind) return;
    const currentCommands = Array.from(grid.children, btn => {
        const text = btn.querySelector('.hk-label');
        return text ? text.textContent : null;
    });
    if (label.textContent === group.label
            && currentCommands.length === group.commands.length
            && group.commands.every((cmd, i) => currentCommands[i] === cmd)) {
        grid.dataset.agentKind = agentKind;
        return;
    }

    const fragment = document.createDocumentFragment();
    group.commands.forEach(cmd => {
        const btn = document.createElement('button');
        btn.className = 'hk-btn hk-cyan';
        const text = document.createElement('span');
        text.className = 'hk-label';
        text.textContent = cmd;
        btn.appendChild(text);
        btn.addEventListener('click', () => typeCmd(cmd));
        fragment.appendChild(btn);
    });
    label.textContent = group.label;
    grid.replaceChildren(fragment);
    grid.dataset.agentKind = agentKind;
}

// Rewrite every sub-label in both surfaces to match the pane on screen.
function applyKeyLabels(target) {
    const t = target || (typeof _termTarget !== 'undefined' ? _termTarget : null);
    if (!t) return;
    const agentKind = _agentKindFor(t);
    const labels = keyLabelsFor(agentKind);

    // Element-level visibility, for controls the null-label rule cannot express.
    // That rule hides a button by saying "this KEY means nothing here", which
    // needs a key: a group label has none, and a prefix button sends composer
    // text rather than a keystroke. Space-separated list of kinds; anything not
    // listed hides the element. The two rules never fight — no element carries
    // both a null label and data-agent-only.
    document.querySelectorAll('[data-agent-only]').forEach(el => {
        const kinds = el.getAttribute('data-agent-only').split(/\s+/);
        el.classList.toggle('hk-hidden', !kinds.includes(agentKind));
    });

    document.querySelectorAll('[data-key]').forEach(btn => {
        const keys = btn.getAttribute('data-key');
        if (!(keys in labels)) return;
        const text = labels[keys];
        btn.classList.toggle('hk-hidden', text === null);
        if (text === null) return;
        const sub = btn.querySelector('.hk-sub');
        if (sub && sub.textContent !== text) sub.textContent = text;
    });

    renderCommandGroup(agentKind);
}
