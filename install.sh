#!/usr/bin/env bash
#
# install.sh — Install Claude Assist (venv, deps, config, CLI command).
#
# Run from inside the repo:
#
#     git clone <repo-url> ~/.local/share/claude-assist
#     cd ~/.local/share/claude-assist
#     ./install.sh
#
# Idempotent — safe to re-run. Never overwrites an existing .env.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- helpers ---------------------------------------------------------------
say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
err()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/claude-assist"
CONFIG_FILE="$CONFIG_DIR/config.env"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
BIN_TARGET="$BIN_DIR/assist"
SRC_BIN="$SCRIPT_DIR/bin/assist"

# ---- [1/7] check prerequisites --------------------------------------------
say "[1/7] Checking prerequisites"

command -v python3 >/dev/null || err "python3 not found"

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')"
[[ "$PY_OK" == "1" ]] || err "Python 3.11+ required (found $PY_VER)"
ok "python3 $PY_VER"

# tmux is required — every core feature (terminal streaming, auto-yes,
# session management, automate) calls tmux directly at startup.
OS="$(uname -s)"

if [[ "$OS" == "Darwin" ]]; then
    command -v tmux >/dev/null || err "tmux not found (install with: brew install tmux)"
else
    command -v tmux >/dev/null || err "tmux not found (install with: sudo apt install tmux)"
fi
ok "tmux $(tmux -V | awk '{print $2}')"

# Clipboard / key-send tools — fallback paths only (tmux path is always preferred).
if [[ "$OS" == "Darwin" ]]; then
    # macOS: pbcopy/pbpaste are built-in; osascript handles folder picker and key-send.
    ok "pbcopy/pbpaste (built-in)"
    command -v osascript >/dev/null && ok "osascript (built-in)" || warn "osascript not found — folder picker and paste fallback disabled"
else
    # Linux: xclip for clipboard, xdotool for X11 key-send, zenity for folder picker.
    if command -v xclip >/dev/null; then
        ok "xclip"
    else
        warn "xclip not found — clipboard copy disabled (install: sudo apt install xclip)"
    fi
    if command -v xdotool >/dev/null; then
        ok "xdotool"
    else
        warn "xdotool not found — X11 key-send disabled (install: sudo apt install xdotool)"
    fi
    if command -v zenity >/dev/null; then
        ok "zenity"
    else
        warn "zenity not found — native folder picker disabled (install: sudo apt install zenity)"
    fi
fi

# claude CLI — primary Claude Code launch mode (default)
if command -v claude >/dev/null; then
    ok "claude binary ($(claude --version 2>/dev/null || echo '?'))"
elif command -v npx >/dev/null; then
    warn "npx found (set claude_mode to 'npx' in Settings if claude CLI not available)"
else
    warn "claude CLI and npx not found — Claude Code launch will fail"
    warn "  install Claude CLI: https://claude.com/claude-code"
    warn "  or Node.js: https://nodejs.org/"
fi

if command -v docker >/dev/null; then
    if docker ps >/dev/null 2>&1; then
        ok "docker (user has access)"
    else
        warn "docker installed but user cannot run it — add user to docker group:"
        warn "  sudo usermod -aG docker \$USER && newgrp docker"
    fi
else
    warn "docker not found — container build/spawn features disabled"
fi

command -v curl >/dev/null && ok "curl" || warn "curl not found — assist CLI will fall back to python for HTTP"

# Repo directory writability — server writes settings, history, state files here
[[ -w "$SCRIPT_DIR" ]] || err "Repo directory is not writable: $SCRIPT_DIR"
ok "repo directory writable"

# ---- [2/7] create venv + install deps --------------------------------------
say "[2/7] Creating Python venv and installing dependencies"

# On Debian/Ubuntu, python3-venv may not be installed
python3 -m venv --help &>/dev/null || err "python3-venv not found (install with: sudo apt install python3-venv)"

if [[ ! -d "$SCRIPT_DIR/.venv" ]]; then
    python3 -m venv "$SCRIPT_DIR/.venv"
    ok "created .venv"
else
    ok ".venv already exists"
fi

"$SCRIPT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$SCRIPT_DIR/.venv/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
ok "installed: $(grep -v '^$' "$SCRIPT_DIR/requirements.txt" | tr '\n' ' ')"

# ---- [3/7] seed .env -------------------------------------------------------
say "[3/7] Seeding .env"

if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    cp "$SCRIPT_DIR/env.example" "$SCRIPT_DIR/.env"
    ok "created .env from env.example"
else
    ok ".env already exists (not overwritten)"
fi

# ---- [4/7] record install location -----------------------------------------
say "[4/7] Recording install location"

mkdir -p "$CONFIG_DIR"
cat > "$CONFIG_FILE" <<EOF
# Claude Assist — user config (written by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ))
# Tells the 'assist' CLI where this repo lives. Safe to edit if you move the repo.
ASSIST_HOME="$SCRIPT_DIR"
EOF
ok "wrote $CONFIG_FILE"

# ---- [5/7] install CLI command ---------------------------------------------
say "[5/7] Installing 'assist' CLI command"

[[ -f "$SRC_BIN" ]] || err "Missing $SRC_BIN — repo is incomplete"

mkdir -p "$BIN_DIR"
chmod +x "$SRC_BIN" "$SCRIPT_DIR/assist-ctl"

if [[ -L "$BIN_TARGET" ]]; then
    existing="$(readlink -f "$BIN_TARGET" || true)"
    if [[ "$existing" == "$SRC_BIN" ]]; then
        ok "$BIN_TARGET already linked"
    else
        ln -sf "$SRC_BIN" "$BIN_TARGET"
        ok "updated symlink $BIN_TARGET"
    fi
elif [[ -e "$BIN_TARGET" ]]; then
    warn "$BIN_TARGET exists and is not a symlink — leaving it alone"
    warn "remove it manually and re-run to install: rm $BIN_TARGET"
else
    ln -s "$SRC_BIN" "$BIN_TARGET"
    ok "created symlink $BIN_TARGET"
fi

case ":$PATH:" in
    *":$BIN_DIR:"*)
        ok "$BIN_DIR is on PATH"
        ;;
    *)
        warn "$BIN_DIR is NOT on your PATH"
        warn "  add this to ~/.bashrc or ~/.zshrc:"
        warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

# ---- [6/7] Claude Code statusline integration (optional) -------------------
say "[6/7] Claude Code statusline integration"

STATUSLINE_BIN="$SCRIPT_DIR/bin/statusline.sh"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
CONTEXT_USAGE="$HOME/.claude/state/context-usage.json"
# Threshold for "recent enough to count as working". One week covers the
# infrequent-Claude-user case without permanently masking a broken setup.
STALE_AFTER_SEC=604800

chmod +x "$STATUSLINE_BIN" 2>/dev/null || true
mkdir -p "$HOME/.claude/state" 2>/dev/null && ok "ensured ~/.claude/state/"

# Read the configured statusLine command (or empty if none/malformed).
read_statusline_cmd() {
    python3 -c "
import json, sys
try:
    d = json.load(open('$CLAUDE_SETTINGS'))
    sl = d.get('statusLine')
    if isinstance(sl, dict): print(sl.get('command', ''))
    elif isinstance(sl, str): print(sl)
except Exception:
    sys.exit(2)
" 2>/dev/null
}

# Replace ~/.claude/settings.json's statusLine with assist's, after backing up.
write_assist_statusline() {
    local backup="$CLAUDE_SETTINGS.bak.$(date +%s)"
    cp "$CLAUDE_SETTINGS" "$backup"
    python3 - "$CLAUDE_SETTINGS" "$STATUSLINE_BIN" <<'PYEOF'
import json, sys
path, cmd = sys.argv[1], sys.argv[2]
with open(path) as f:
    d = json.load(f)
d['statusLine'] = {'type': 'command', 'command': cmd, 'padding': 0}
with open(path, 'w') as f:
    json.dump(d, f, indent=2)
PYEOF
    ok "  backed up old settings to: $backup"
}

# Returns 0 if context-usage.json is present and updated within STALE_AFTER_SEC.
context_usage_is_fresh() {
    [[ -f "$CONTEXT_USAGE" ]] || return 1
    local mtime now age
    mtime="$(stat -c%Y "$CONTEXT_USAGE" 2>/dev/null || stat -f%m "$CONTEXT_USAGE" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    age=$(( now - mtime ))
    [[ $age -lt $STALE_AFTER_SEC ]]
}

# Prompt for replacement. Honors ASSIST_REPLACE_STATUSLINE=1 for non-interactive
# runs and defaults to "no" when there's no TTY.
prompt_replace_statusline() {
    if [[ "${ASSIST_REPLACE_STATUSLINE:-0}" == "1" ]]; then
        echo "y"; return
    fi
    if [[ -t 0 ]]; then
        local ans=""
        read -r -p "    Replace it with Assist's bin/statusline.sh? [y/N] " ans
        echo "${ans:-n}"
    else
        echo "n"
    fi
}

if [[ ! -f "$CLAUDE_SETTINGS" ]]; then
    warn "~/.claude/settings.json not found — Claude Code may not be installed yet"
    warn "  after installing Claude Code, add statusLine to get 'i' button context data:"
    warn "  \"statusLine\": {\"type\": \"command\", \"command\": \"$STATUSLINE_BIN\"}"
else
    sl_cmd="$(read_statusline_cmd)" || sl_cmd="__parse_error__"

    if [[ "$sl_cmd" == "__parse_error__" ]]; then
        warn "could not parse $CLAUDE_SETTINGS — add statusLine manually:"
        warn "  \"statusLine\": {\"type\": \"command\", \"command\": \"$STATUSLINE_BIN\"}"
    elif [[ -z "$sl_cmd" ]]; then
        # No statusLine — install ours.
        write_assist_statusline
        ok "added statusLine to $CLAUDE_SETTINGS"
        ok "  using $STATUSLINE_BIN"
    elif [[ "$sl_cmd" == "$STATUSLINE_BIN" ]]; then
        ok "Claude Code statusLine already set to Assist's bin/statusline.sh"
    else
        # A custom statusLine is configured. The 'i' panel only works if SOMETHING
        # is keeping ~/.claude/state/context-usage.json fresh — check that directly,
        # which is robust to project-local overrides we can't see from here.
        if context_usage_is_fresh; then
            ok "Claude Code statusLine is configured and ~/.claude/state/context-usage.json is recent"
            ok "  ($sl_cmd)"
        else
            warn "Claude Code statusLine is configured but ~/.claude/state/context-usage.json is missing or stale:"
            warn "    command: $sl_cmd"
            if [[ -f "$CONTEXT_USAGE" ]]; then
                warn "    last updated: $(date -d "@$(stat -c%Y "$CONTEXT_USAGE")" '+%Y-%m-%d %H:%M' 2>/dev/null || stat -f%Sm "$CONTEXT_USAGE")"
            else
                warn "    file does not exist"
            fi
            warn "    The Assist 'i' button will be blank without it."
            ans="$(prompt_replace_statusline)"
            if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
                write_assist_statusline
                ok "replaced statusLine with $STATUSLINE_BIN"
                ok "  the 'i' panel will populate after Claude Code runs once"
            else
                warn "    skipped — set ASSIST_REPLACE_STATUSLINE=1 to auto-replace next time, or update manually:"
                warn "    \"statusLine\": {\"type\": \"command\", \"command\": \"$STATUSLINE_BIN\"}"
            fi
        fi
    fi
fi

# ---- [7/7] done ------------------------------------------------------------
say "[7/7] Install complete"
cat <<EOF

Next steps:
  1. (optional) Edit .env to customize ports / paths
       $SCRIPT_DIR/.env
  2. Start the server:
       assist start
  3. Open http://localhost:${ASSIST_PORT:-8089} in a browser
  4. (optional) Run diagnostics:
       assist doctor
  5. (optional) Build the container image:
       assist container build
  6. See all commands:
       assist help

EOF
