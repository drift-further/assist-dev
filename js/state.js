// state.js — Global variables, constants, and DOM refs
// Loaded first; all other modules depend on these globals.

// Settings — fetched from /api/settings on load, updated on save.
let SETTINGS = null;  // populated by loadSettings()
let CLAUDE_CMD = 'npx @anthropic-ai/claude-code';  // default until settings load
let _serverPid = null;
let _serverUptime = 0;

async function loadSettings() {
    try {
        const r = await fetch('/api/settings');
        if (r.ok) {
            const d = await r.json();
            SETTINGS = d.settings;
            _serverPid = d.pid;
            _serverUptime = d.uptime;
            // Derive CLAUDE_CMD for backwards compat
            const mode = SETTINGS.server.claude_mode;
            const cmds = { npx: 'npx @anthropic-ai/claude-code', claude: 'claude' };
            CLAUDE_CMD = cmds[mode] || cmds.npx;
        }
    } catch(e) {}
}

// Load on startup
loadSettings();

const input = document.getElementById('text-input');
const flash = document.getElementById('flash');
const dot = document.getElementById('status-dot');
const statusTime = document.getElementById('status-time');
const listArea = document.getElementById('list-area');
const tmuxInd = document.getElementById('tmux-ind');
const routeDot = document.getElementById('route-dot');
const routeLabel = document.getElementById('route-label');
const routeAttach = document.getElementById('route-attach');

let lastAction = null;
let _history = [];
let _favorites = [];

// Terminal state
let _termOpen = true;           // always open in drawer layout
let _termTarget = null;         // active tmux target
let _termPaused = false;        // scroll-freeze
let _termLines = 2000;          // scrollback depth (10x default)
let _termPollTimer = null;
let _termLatestContent = '';    // stored when paused
let _termHasNew = false;
let _termLastContent = '';      // last displayed content
let _termProjects = [];
let _termShowProjects = false;

// WebSocket state
let _termWs = null;             // WebSocket instance
let _termWsConnected = false;   // true when WS is active
let _termWsReconnectTimer = null;

// Smart actions state
let _smartActionsKey = '';
let _smartDismissed = null;
let _smartActionTarget = null;  // which pane the smart action came from (main or split)
let _layoutShifting = false;    // true during smart-actions show/hide to suppress scroll-freeze

// Input routing: when true, keyboard input goes to split pane instead of main
let _inputToSplit = false;

function getInputTarget() {
    if (_inputToSplit) {
        const session = _termTarget ? _termTarget.split(':')[0] : '';
        const state = _splitPanes[session];
        if (state) return state.target;
    }
    return _termTarget;
}

// Session state
let _sessionPanes = [];
let _sessionPrompts = {};       // target -> true if prompt detected
let _sessionStates = {};        // target -> { state, since, prevState }
// _sessionRefreshTimer removed — session refresh handled by consolidatedPoll()

// Input state
let _sending = false;
let _attachedFile = null;
let _attachedFilePath = null;   // pre-uploaded path (clipboard images)

// Notification state
let _notifPermission = typeof Notification !== 'undefined' ? Notification.permission : 'denied';
let _notifSentFor = {};         // target -> last notified tail hash
let _notifScanTimer = null;

// Info toast timer
let _infoTimer = null;

// Saved commands state
let _splitPanes = {};              // session -> { target, ws, wsConnected, lastContent, label }
let _cmdPanelOpen = false;
let _cmdOutputOpen = false;
let _currentCommands = [];
let _currentCommandsProject = '';

// Content-based activity detection (more reliable than tmux session_activity)
let _activityDecayTimers = {};  // target -> setTimeout id
let _lastScanContent = {};      // target -> last scanned tail (for change detection)

// Per-project automation settings
let _projectSettings = null;
let _projectSettingsName = '';
let _projSettingsOpen = false;

// Idle fade: suppress for first few poll cycles so content detection can establish baseline
const _pageLoadedAt = Date.now();


