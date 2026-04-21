#!/bin/bash

echo "🔧 Initializing Claude Code environment with Host Tools..."

# Fix home directory permissions for developer user
echo "🔧 Ensuring proper permissions for developer user..."
mkdir -p /home/developer/.claude /home/developer/.config/claude

# Copy Claude configuration from host if mounted
if [ -d "/host-claude-config" ]; then
    echo "📋 Copying Claude configuration from host..."
    
    # Copy .claude.json if it exists
    if [ -f "/host-claude-config/.claude.json" ]; then
        cp /host-claude-config/.claude.json /home/developer/.claude.json
        echo "✅ Claude settings copied from ~/.claude.json"
        # Debug: show if API key is present
        if grep -q "api_key" /home/developer/.claude.json 2>/dev/null; then
            echo "   ✓ API key found in configuration"
        fi
        # Pre-accept /workspace trust dialogs — container is ephemeral and sandboxed,
        # and relaunches would otherwise hit the trust prompt on every iteration.
        if command -v jq &>/dev/null; then
            jq '.projects["/workspace"] = ((.projects["/workspace"] // {}) + {
                "hasTrustDialogAccepted": true,
                "hasTrustDialogBashAccepted": true,
                "hasTrustDialogHooksAccepted": true,
                "hasCompletedProjectOnboarding": true,
                "hasClaudeMdExternalIncludesApproved": true,
                "hasClaudeMdExternalIncludesWarningShown": true
            })' /home/developer/.claude.json > /tmp/claude-json-patched && \
                mv /tmp/claude-json-patched /home/developer/.claude.json
            echo "🔓 /workspace trust dialogs pre-accepted"
        fi
    else
        echo "⚠️  No ~/.claude.json found on host - you may need to login"
    fi
    
    # Copy host .claude directory to container-local writable copy (host mount is read-only)
    # Skip projects/, sessions/, .credentials.json — these are bind-mounted RW directly
    if [ -d "/host-claude-home" ] && [ "$(ls -A /host-claude-home 2>/dev/null)" ]; then
        # Copy everything EXCEPT RW-mounted paths (they're already bind-mounted from host)
        for item in /host-claude-home/*; do
            base=$(basename "$item")
            case "$base" in
                projects|sessions) continue ;;  # RW bind-mounted
            esac
            cp -r "$item" "/home/developer/.claude/$base" 2>/dev/null || true
        done
        # Copy dotfiles (settings, credentials if not already mounted, etc.)
        for item in /host-claude-home/.*; do
            base=$(basename "$item")
            case "$base" in
                .|..) continue ;;
                .credentials.json)
                    # Only copy if not already bind-mounted
                    [ -f "/home/developer/.claude/.credentials.json" ] && continue ;;
            esac
            cp -r "$item" "/home/developer/.claude/$base" 2>/dev/null || true
        done
        echo "🔒 Claude directory copied from host (RO base, RW overlays for sessions)"
        if [ -f "/home/developer/.claude/.credentials.json" ]; then
            echo "   ✓ Credentials file found!"
        fi
    elif [ -d "/host-claude-config/.claude" ]; then
        cp -r /host-claude-config/.claude/* /home/developer/.claude/ 2>/dev/null || true
        echo "✅ Claude directory copied (includes credentials)"
        if [ -f "/home/developer/.claude/.credentials.json" ]; then
            echo "   ✓ Credentials file found!"
        fi
    fi
    
    # Copy entire Claude config directory structure if it exists
    if [ -d "/host-claude-config/.config/claude" ]; then
        mkdir -p /home/developer/.config/claude
        cp -r /host-claude-config/.config/claude/* /home/developer/.config/claude/ 2>/dev/null || true
        echo "✅ Claude config directory copied from ~/.config/claude"
        if [ -f "/home/developer/.config/claude/settings.json" ]; then
            echo "   ✓ MCP server configurations loaded"
        fi
    else
        echo "ℹ️  No ~/.config/claude directory on host (MCP servers may not be configured)"
    fi
else
    echo "⚠️  ERROR: Claude configuration not mounted from host!"
    echo "   This should not happen - check your Docker mount"
    echo '{}' > /home/developer/.claude.json
fi

# Verify skill symlinks resolve (warn on broken ones)
if [ -d "/home/developer/.claude/skills" ]; then
    for skill in /home/developer/.claude/skills/*/; do
        if [ -L "$skill" ] && [ ! -e "$skill" ]; then
            target=$(readlink "$skill")
            echo "⚠️  Broken skill symlink: $(basename $skill) → $target"
        fi
    done
fi

# Determine Claude launch command: env var > native binary > npx fallback
if [ -n "${CLAUDE_CMD:-}" ]; then
    _CLAUDE_BIN="$CLAUDE_CMD"
elif [ -f "/home/developer/.local/bin/claude" ]; then
    _CLAUDE_BIN="/home/developer/.local/bin/claude"
else
    _CLAUDE_BIN="npx @anthropic-ai/claude-code"
fi
echo "✅ Claude Code: $_CLAUDE_BIN"
CLAUDE_VER=$(su developer -c "$_CLAUDE_BIN --version" 2>/dev/null | head -1) && echo "   Version: $CLAUDE_VER" || true

# Set proper ownership
chown -R developer:developer /home/developer
chmod 755 /home/developer /home/developer/.claude /home/developer/.config
chmod -R 644 /home/developer/.claude.json 2>/dev/null || true
chmod -R 644 /home/developer/.config/claude/* 2>/dev/null || true

# Ensure Playwright browser directory is writable by developer user
if [ -d "/ms-playwright" ]; then
    chown -R developer:developer /ms-playwright
    chmod -R 755 /ms-playwright
    echo "✅ Playwright browser directory permissions set"
fi

# Start virtual display for browser support
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 &
fluxbox > /dev/null 2>&1 &

# Auto-activate .venv if it exists
VENV_ACTIVATED=false
if [ -f ".venv/bin/activate" ]; then
    echo "✅ Activating Python virtual environment (.venv)..."
    source .venv/bin/activate
    echo "🐍 Python venv activated: $(which python)"
    VENV_ACTIVATED=true
else
    echo "ℹ️  No .venv found in current directory"
fi

# Auto-install Python dependencies from requirements.txt
if [ -f "requirements.txt" ]; then
    echo "📦 Found requirements.txt - installing Python dependencies..."
    
    # Check if requirements have changed or been installed before
    REQUIREMENTS_HASH=$(md5sum requirements.txt | cut -d' ' -f1)
    REQUIREMENTS_INSTALLED_FILE=".requirements_installed_${REQUIREMENTS_HASH}"
    
    if [ ! -f "$REQUIREMENTS_INSTALLED_FILE" ]; then
        echo "   Installing/updating Python packages..."
        
        # Use appropriate pip command based on venv activation
        if [ "$VENV_ACTIVATED" = true ]; then
            pip install -r requirements.txt
        else
            # Install globally if no venv (as developer user)
            python3 -m pip install --user -r requirements.txt
        fi
        
        if [ $? -eq 0 ]; then
            # Mark requirements as installed with this hash
            touch "$REQUIREMENTS_INSTALLED_FILE"
            # Clean up old requirement markers
            rm -f .requirements_installed_* 2>/dev/null || true
            touch "$REQUIREMENTS_INSTALLED_FILE"
            echo "✅ Python dependencies installed successfully"
        else
            echo "⚠️  Some Python dependencies failed to install"
        fi
    else
        echo "✅ Python dependencies already up-to-date"
    fi
else
    # Check for other Python dependency files
    if [ -f "pyproject.toml" ]; then
        echo "📦 Found pyproject.toml - installing project with dependencies..."
        if [ "$VENV_ACTIVATED" = true ]; then
            pip install -e .
        else
            python3 -m pip install --user -e .
        fi
        
        if [ $? -eq 0 ]; then
            echo "✅ Python project installed successfully"
        else
            echo "⚠️  Python project installation failed"
        fi
    elif [ -f "setup.py" ]; then
        echo "📦 Found setup.py - installing project with dependencies..."
        if [ "$VENV_ACTIVATED" = true ]; then
            pip install -e .
        else
            python3 -m pip install --user -e .
        fi
        
        if [ $? -eq 0 ]; then
            echo "✅ Python project installed successfully"
        else
            echo "⚠️  Python project installation failed"
        fi
    else
        echo "ℹ️  No Python dependency files found (requirements.txt, pyproject.toml, setup.py)"
    fi
fi

# Install per-project packages from Assist container config
# (written by Assist to /tmp/assist-project-packages.txt before container start)
if [ -f "/tmp/assist-project-packages.txt" ]; then
    echo "📦 Installing per-project packages from Assist..."
    while IFS= read -r pkg; do
        pkg=$(echo "$pkg" | xargs)  # trim whitespace
        [ -z "$pkg" ] && continue
        echo "   Installing: $pkg"
        if [ "$VENV_ACTIVATED" = true ]; then
            pip install "$pkg" 2>&1 | tail -1
        else
            python3 -m pip install --user "$pkg" 2>&1 | tail -1
        fi
    done < /tmp/assist-project-packages.txt
    echo "✅ Per-project packages installed"
fi

# Check for mounted Flutter from host
if [ -d "/host-flutter" ]; then
    FLUTTER_FOUND=false
    
    # Standard Flutter installation (most common)
    if [ -f "/host-flutter/bin/flutter" ]; then
        export FLUTTER_ROOT="/host-flutter"
        export PATH="/host-flutter/bin:/host-flutter/bin/cache/dart-sdk/bin:$PATH"
        export PUB_CACHE="${PUB_CACHE:-/home/developer/.pub-cache}"
        FLUTTER_FOUND=true
        echo "✅ Host Flutter available (standard): $(/host-flutter/bin/flutter --version 2>&1 | head -n1)"
    # Snap installations - try common paths
    elif [ -f "/host-flutter/common/flutter/bin/flutter" ]; then
        export FLUTTER_ROOT="/host-flutter/common/flutter"
        export PATH="/host-flutter/common/flutter/bin:/host-flutter/common/flutter/bin/cache/dart-sdk/bin:$PATH"
        export PUB_CACHE="${PUB_CACHE:-/home/developer/.pub-cache}"
        FLUTTER_FOUND=true
        echo "✅ Host Flutter available (snap common): $(/host-flutter/common/flutter/bin/flutter --version 2>&1 | head -n1)"
    elif [ -f "/host-flutter/current/flutter/bin/flutter" ]; then
        export FLUTTER_ROOT="/host-flutter/current/flutter"
        export PATH="/host-flutter/current/flutter/bin:/host-flutter/current/flutter/bin/cache/dart-sdk/bin:$PATH"
        export PUB_CACHE="${PUB_CACHE:-/home/developer/.pub-cache}"
        FLUTTER_FOUND=true
        echo "✅ Host Flutter available (snap current): $(/host-flutter/current/flutter/bin/flutter --version 2>&1 | head -n1)"
    elif [ -f "/host-flutter/current/bin/flutter" ]; then
        export FLUTTER_ROOT="/host-flutter/current"
        export PATH="/host-flutter/current/bin:/host-flutter/current/bin/cache/dart-sdk/bin:$PATH"
        export PUB_CACHE="${PUB_CACHE:-/home/developer/.pub-cache}"
        FLUTTER_FOUND=true
        echo "✅ Host Flutter available (snap bin): $(/host-flutter/current/bin/flutter --version 2>&1 | head -n1)"
    fi
    
    if [ "$FLUTTER_FOUND" = true ]; then
        # Verify Dart SDK is accessible
        if command -v dart >/dev/null 2>&1; then
            echo "✅ Dart SDK available: $(dart --version 2>&1)"
        else
            echo "⚠️  Dart SDK not found in Flutter cache"
        fi
        
        # Create pub-cache directory if it doesn't exist
        mkdir -p /home/developer/.pub-cache
        chown developer:developer /home/developer/.pub-cache
    else
        echo "⚠️  Flutter mounted but binary not found"
        echo "   Directory contents:"
        ls -la /host-flutter/ 2>/dev/null | head -10
    fi
else
    echo "ℹ️  Flutter not mounted from host system"
    echo "🔍 Checking for built-in Flutter installation..."
    
    # Check if Flutter was installed during container build
    if [ -f "/opt/flutter/bin/flutter" ]; then
        export FLUTTER_ROOT="/opt/flutter"
        export PATH="/opt/flutter/bin:/opt/flutter/bin/cache/dart-sdk/bin:$PATH"
        export PUB_CACHE="${PUB_CACHE:-/home/developer/.pub-cache}"
        
        # Create pub-cache directory if it doesn't exist
        mkdir -p /home/developer/.pub-cache
        chown developer:developer /home/developer/.pub-cache
        
        echo "✅ Built-in Flutter available: $(flutter --version 2>&1 | head -n1)"
        
        # Verify Dart SDK is accessible
        if command -v dart >/dev/null 2>&1; then
            echo "✅ Dart SDK available: $(dart --version 2>&1)"
        fi
    else
        echo "   To use Flutter, install it on your host at one of:"
        echo "   • /opt/flutter"
        echo "   • ~/flutter"
        echo "   • /usr/local/flutter"
        echo "   Or rebuild this container with Flutter included"
    fi
fi

# SECURITY: Docker access intentionally disabled
# This container cannot manage host containers (prevents accidental deletion of PostgreSQL, etc.)
echo "🔒 Docker access: DISABLED (security restriction)"

# Set browser environment variables for Playwright
export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# Set NODE_PATH to help MCP servers find globally installed packages
export NODE_PATH="/usr/local/lib/node_modules:/opt/mcp-workspace/node_modules:$NODE_PATH"

# Add global MCP workspace to PATH for any MCP server binaries
export PATH="/opt/mcp-workspace/node_modules/.bin:$PATH"

# Ensure MCP Playwright environment is ready
echo "🎭 Setting up Playwright for MCP servers..."
if [ -d "/opt/mcp-workspace/node_modules/playwright" ]; then
    echo "✅ Playwright dependency available for MCP servers"
    # Ensure browsers are accessible
    if [ -d "/ms-playwright" ] && [ "$(ls -A /ms-playwright 2>/dev/null)" ]; then
        echo "✅ Playwright browsers ready for MCP usage"
    else
        echo "⚠️  Playwright browsers not found, MCP may need to install them"
    fi
else
    echo "⚠️  Playwright not found in MCP workspace"
fi

# Inject container-specific settings into Claude config
# Strip permissions (container uses --dangerously-skip-permissions) and host-specific hooks
SETTINGS_FILE="/home/developer/.claude/settings.json"
if [ -f "$SETTINGS_FILE" ] && command -v jq &>/dev/null; then
    jq '
        # Remove permissions — container runs with --dangerously-skip-permissions
        del(.permissions)
        # Remove host hooks — they reference host paths that dont exist in container
        | del(.hooks)
        # Remove MCP servers that reference host paths (they cant run in container)
        | del(.mcpServers)
        # Enable agent teams (experimental) in in-process mode (no tmux in container)
        | .env = (.env // {}) + {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}
        | .teammateMode = "in-process"
        # Statusline: context usage + model name
        | .statusLine = {"type": "command", "command": "/usr/local/bin/claude-statusline", "padding": 0}
    ' "$SETTINGS_FILE" > /tmp/claude-settings-updated.json && \
        mv /tmp/claude-settings-updated.json "$SETTINGS_FILE"
    echo "✅ Agent teams enabled (in-process mode)"
else
    echo "⚠️  Could not process settings (jq missing or settings not found)"
fi

# Strip permissions and hooks from workspace project settings (host paths, not relevant in container)
for _proj_settings in /workspace/.claude/settings.json /workspace/.claude/settings.local.json; do
    if [ -f "$_proj_settings" ] && command -v jq &>/dev/null; then
        jq 'del(.permissions) | del(.hooks)' "$_proj_settings" > /tmp/_proj_settings_clean.json && \
            mv /tmp/_proj_settings_clean.json "$_proj_settings"
    fi
done
echo "🔓 Permissions/hooks/MCP stripped (container uses --dangerously-skip-permissions)"

# Display environment info
echo ""
echo "📁 Working directory: $(pwd)"
echo "🔧 Available tools:"
echo "   • Claude Code with full permissions"
if [ "$VENV_ACTIVATED" = true ]; then
    echo "   • Python $(python --version 2>&1 | cut -d' ' -f2) (virtual environment)"
else
    echo "   • Python $(python3 --version 2>&1 | cut -d' ' -f2)"
fi
echo "   • Node $(node --version)"
if [ -d "/host-flutter/bin" ]; then
    echo "   • Flutter (from host system)"
elif [ -f "/opt/flutter/bin/flutter" ]; then
    echo "   • Flutter $(flutter --version 2>&1 | grep '^Flutter' | cut -d' ' -f2) (built-in)"
fi
echo "   • Browser support: Chromium, Firefox (with virtual display)"
echo "   • Agent teams: enabled (in-process mode)"
echo "   • 🔒 Docker: DISABLED (security)"
echo "   • 🔒 sudo rm: BLOCKED (security)"
echo "   • Auto Python dependency installation (requirements.txt, pyproject.toml, setup.py)"
if [ -f "/home/developer/.config/claude/settings.json" ]; then
    echo "   • MCP servers configured from host"
fi

echo ""

# Optional: Update Claude CLI on container start (native installer handles updates)
if [ "${CLAUDE_AUTO_UPDATE:-false}" = "true" ]; then
    echo "🔄 Checking for Claude CLI updates..."
    UPDATE_OUTPUT=$(su developer -c "$_CLAUDE_BIN update" 2>&1)
    if echo "$UPDATE_OUTPUT" | grep -q "Successfully updated"; then
        echo "✅ $(echo "$UPDATE_OUTPUT" | grep "Successfully updated")"
    elif echo "$UPDATE_OUTPUT" | grep -q "already"; then
        echo "✅ Claude CLI is already up to date"
    else
        CURRENT_VERSION=$(echo "$UPDATE_OUTPUT" | grep -i "version" | head -1)
        [ -n "$CURRENT_VERSION" ] && echo "✅ $CURRENT_VERSION"
    fi
fi

echo "🤖 Starting Claude Code with full permissions..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Ensure we start in the workspace directory
cd /workspace

# Clean up any old project agents from previous sessions
rm -f /home/developer/.claude/agents/project-*.md 2>/dev/null

# Handle project agents BEFORE switching to developer (as root for proper permissions)
# Use CLAUDE_AGENT_MODE environment variable to control behavior (symlink or copy)
# Default to symlink for live updates
AGENT_MODE="${CLAUDE_AGENT_MODE:-symlink}"

if [ -d "/workspace/.claude/agents" ] && [ "$(ls -A /workspace/.claude/agents 2>/dev/null)" ]; then
    if [ "$AGENT_MODE" = "symlink" ]; then
        echo "🔗 Symlinking project agents from $(pwd)..."
        for agent in /workspace/.claude/agents/*; do
            if [ -f "$agent" ]; then
                agent_name=$(basename "$agent")
                agent_base="${agent_name%.md}"
                ln -sf "$agent" "/home/developer/.claude/agents/project-${agent_base}.md"
                echo "   ✓ Symlinked project agent: $agent_name"
            fi
        done
        # Fix ownership of symlinks
        chown -h developer:developer /home/developer/.claude/agents/project-*.md 2>/dev/null || true
    else
        echo "📋 Copying project agents from $(pwd)..."
        for agent in /workspace/.claude/agents/*; do
            if [ -f "$agent" ]; then
                agent_name=$(basename "$agent")
                agent_base="${agent_name%.md}"
                cp "$agent" "/home/developer/.claude/agents/project-${agent_base}.md"
                echo "   ✓ Copied project agent: $agent_name"
            fi
        done
        # Fix ownership of copied files
        chown developer:developer /home/developer/.claude/agents/project-*.md 2>/dev/null || true
    fi
else
    echo "ℹ️  No project agents found in $(pwd)/.claude/agents"
fi

# Switch to developer user and run Claude with dangerous skip permissions
# Use full path — su resets PATH and doesn't inherit Docker ENV
# Use printf %q to shell-escape each arg (handles parens, spaces, quotes, etc.)
_QUOTED_ARGS=""
for _arg in "$@"; do
    _QUOTED_ARGS="$_QUOTED_ARGS $(printf '%q' "$_arg")"
done
exec su developer -c "cd /workspace && $_CLAUDE_BIN --dangerously-skip-permissions$_QUOTED_ARGS"