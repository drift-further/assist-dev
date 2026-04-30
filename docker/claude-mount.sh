#!/bin/bash
# Claude Docker with Direct Mount of Credentials
# Launches Claude Code inside an isolated Docker container with host credential mounts.
# Used by Assist's Automate Run feature.

# Parse command line options
CLAUDE_AUTO_UPDATE=false
SKIP_VERSION_CHECK=false
REBUILD_IMAGE=false
while getopts "unbsh" opt; do
    case $opt in
        u) CLAUDE_AUTO_UPDATE=true ;;
        n) SKIP_VERSION_CHECK=true ;;  # Skip automatic version check
        b) REBUILD_IMAGE=true ;;       # Rebuild Docker image before starting
        s) SKIP_VERSION_CHECK=true ;;  # Alias for skip
        h)
            echo "Usage: claude-mount [-u] [-n] [-b] [claude args...]"
            echo "  -u    Force Claude CLI update before starting"
            echo "  -n    Skip automatic version check (faster startup)"
            echo "  -b    Rebuild Docker image before starting"
            echo "  -h    Show this help message"
            exit 0
            ;;
        \?) echo "Invalid option: -$OPTARG" >&2; exit 1 ;;
    esac
done
shift $((OPTIND-1))
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSIST_DIR="$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# Read container config from JSON (with jq fallback to defaults)
# Config lives at the assist repo root (one level up from docker/)
# ---------------------------------------------------------------------------
CONFIG_FILE="$ASSIST_DIR/container_config.json"

_cfg() {
    # Read a dotted key from container_config.json, fallback to $2
    if [ -f "$CONFIG_FILE" ] && command -v jq &>/dev/null; then
        val=$(jq -r "$1 // empty" "$CONFIG_FILE" 2>/dev/null)
        [ -n "$val" ] && [ "$val" != "null" ] && echo "$val" && return
    fi
    echo "$2"
}

IMAGE_NAME=$(_cfg '.image.name' 'claude-assist-container')
MEMORY_LIMIT=$(_cfg '.resources.memory' '16g')
CPU_LIMIT=$(_cfg '.resources.cpus' '4')
PIDS_LIMIT=$(_cfg '.resources.pids_limit' '512')
BIND_ADDRESS=$(_cfg '.network.bind_address' '127.0.0.1')
GATEWAY_HOST=$(_cfg '.network.gateway_host' '10.0.0.101')
CLI_PROXY_ENABLED=$(_cfg '.cli_proxy.enabled' 'false')
CLI_PROXY_NAME=$(_cfg '.cli_proxy.container_command' '')
[ "$CLI_PROXY_ENABLED" != "true" ] && CLI_PROXY_NAME=""

# Handle image rebuild
if [ "$REBUILD_IMAGE" = true ]; then
    echo "Rebuilding $IMAGE_NAME image..."
    if docker build --no-cache \
        --build-arg "CLI_PROXY_NAME=$CLI_PROXY_NAME" \
        -t "$IMAGE_NAME" "$SCRIPT_DIR"; then
        echo "Image rebuilt successfully"
    else
        echo "Image build failed"
        exit 1
    fi
fi

echo "Starting Claude with direct credential mount..."

# ── Network isolation setup ──────────────────────────────────────
# Creates a Docker network that allows internet but blocks LAN access
CLAUDE_NETWORK="claude-internet-only"
CLAUDE_SUBNET="172.16.255.0/24"

ensure_network() {
    if ! docker network inspect "$CLAUDE_NETWORK" &>/dev/null; then
        echo "Creating isolated network ($CLAUDE_NETWORK)..."
        if ! docker network create --driver bridge --subnet "$CLAUDE_SUBNET" "$CLAUDE_NETWORK" >/dev/null; then
            echo "Failed to create network $CLAUDE_NETWORK (subnet $CLAUDE_SUBNET may conflict)"
            echo "   Run: docker network inspect \$(docker network ls -q) --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}}{{end}}'"
            exit 1
        fi
    fi

    # Apply iptables rules if not already present (requires sudo, idempotent)
    if ! sudo iptables -C DOCKER-USER -s "$CLAUDE_SUBNET" -d "$CLAUDE_SUBNET" -j ACCEPT 2>/dev/null; then
        echo "Applying network isolation rules (internet only, no LAN)..."
        # Allow traffic within the container subnet (container <-> gateway for DNS/NAT)
        sudo iptables -I DOCKER-USER -s "$CLAUDE_SUBNET" -d "$CLAUDE_SUBNET" -j ACCEPT
        # Allow PostgreSQL access to host (for project databases)
        sudo iptables -I DOCKER-USER -s "$CLAUDE_SUBNET" -d 10.0.0.101 -p tcp --dport 5432 -j ACCEPT
        # Allow Assist access (host CLI proxy on port 8089 — see /api/cli-proxy)
        sudo iptables -I DOCKER-USER -s "$CLAUDE_SUBNET" -d 10.0.0.101 -p tcp --dport 8089 -j ACCEPT
        # Block all RFC1918 private networks and link-local
        sudo iptables -A DOCKER-USER -s "$CLAUDE_SUBNET" -d 10.0.0.0/8 -j DROP
        sudo iptables -A DOCKER-USER -s "$CLAUDE_SUBNET" -d 172.16.0.0/12 -j DROP
        sudo iptables -A DOCKER-USER -s "$CLAUDE_SUBNET" -d 192.168.0.0/16 -j DROP
        sudo iptables -A DOCKER-USER -s "$CLAUDE_SUBNET" -d 169.254.0.0/16 -j DROP
    fi
}

ensure_network

# Automatic version detection (unless -u already set or -n to skip)
if [ "$CLAUDE_AUTO_UPDATE" = false ] && [ "$SKIP_VERSION_CHECK" = false ]; then
    VERSION_CACHE_DIR="$HOME/.cache/claude-mount"
    VERSION_CACHE="$VERSION_CACHE_DIR/latest_version"
    CACHE_MAX_AGE=86400  # 24 hours in seconds

    # Check if cache exists and is fresh
    CACHE_AGE=999999
    if [ -f "$VERSION_CACHE" ]; then
        CACHE_AGE=$(($(date +%s) - $(stat -c %Y "$VERSION_CACHE" 2>/dev/null || echo 0)))
    fi

    # Refresh cache if stale (background to not block startup)
    if [ $CACHE_AGE -gt $CACHE_MAX_AGE ]; then
        mkdir -p "$VERSION_CACHE_DIR"
        # Quick npm check with timeout to avoid blocking
        LATEST_VERSION=$(timeout 3 npm view @anthropic-ai/claude-code version 2>/dev/null)
        if [ -n "$LATEST_VERSION" ]; then
            echo "$LATEST_VERSION" > "$VERSION_CACHE"
        fi
    else
        LATEST_VERSION=$(cat "$VERSION_CACHE" 2>/dev/null)
    fi

    # Get CONTAINER's installed version (not host's!)
    if [ -n "$LATEST_VERSION" ]; then
        # Quick check of container's Claude version (timeout to avoid delays)
        CURRENT_VERSION=$(timeout 5 docker run --rm --entrypoint="" "$IMAGE_NAME" \
            npm list -g --depth=0 2>/dev/null | grep -oP '@anthropic-ai/claude-code@\K[\d.]+' || echo "")

        # Fallback to host version if container check fails
        if [ -z "$CURRENT_VERSION" ]; then
            CURRENT_VERSION=$(claude --version 2>/dev/null | grep -oP '^\d+\.\d+\.\d+' | head -1)
        fi

        if [ -n "$CURRENT_VERSION" ] && [ "$CURRENT_VERSION" != "$LATEST_VERSION" ]; then
            echo "Claude CLI update available: $CURRENT_VERSION -> $LATEST_VERSION"
            CLAUDE_AUTO_UPDATE=true
        else
            echo "Claude CLI is up to date ($CURRENT_VERSION)"
        fi
    fi
fi

if [ "$CLAUDE_AUTO_UPDATE" = true ]; then
    echo "Auto-update enabled"
fi

# Detect if we're in a TTY
if [ -t 0 ] && [ -t 1 ]; then
    TTY_FLAGS="-it"
    echo "Running in interactive mode"
else
    TTY_FLAGS="-i"
    echo "Running in non-TTY mode"
fi

# Generate a unique session ID based on current directory
SESSION_ID=$(echo "$(pwd)" | md5sum | cut -c1-8)
CONTAINER_NAME="claude-session-$SESSION_ID"

# Build the Docker command dynamically based on what exists
DOCKER_CMD="docker run $TTY_FLAGS --rm --name $CONTAINER_NAME"
DOCKER_CMD="$DOCKER_CMD -v \"$(pwd)\":/workspace"

# Mount message bridge directory (Claude writes, host reads)
MESSAGES_DIR="$HOME/.claude-messages"
mkdir -p "$MESSAGES_DIR/to-discord" "$MESSAGES_DIR/to-claude"
DOCKER_CMD="$DOCKER_CMD -v \"$MESSAGES_DIR:/home/developer/.claude-messages\""

# Mount Claude JSON if it exists (mount to where entrypoint expects it)
if [ -f "$HOME/.claude.json" ]; then
    DOCKER_CMD="$DOCKER_CMD -v \"$HOME/.claude.json:/host-claude-config/.claude.json:ro\""
    echo "Mounting .claude.json (read-only)"
fi

# Mount Claude directory: bulk read-only, with RW overlays for session persistence
if [ -d "$HOME/.claude" ]; then
    DOCKER_CMD="$DOCKER_CMD -v \"$HOME/.claude:/host-claude-home:ro\""
    echo "Mounting .claude directory (read-only base)"

    # RW mounts for session persistence — enables `claude --resume` on host after container exit
    mkdir -p "$HOME/.claude/projects" "$HOME/.claude/sessions"
    DOCKER_CMD="$DOCKER_CMD -v \"$HOME/.claude/projects:/home/developer/.claude/projects\""
    DOCKER_CMD="$DOCKER_CMD -v \"$HOME/.claude/sessions:/home/developer/.claude/sessions\""
    echo "Session data mounted read-write (projects/, sessions/)"

    # RW mount for credential refresh persistence
    if [ -f "$HOME/.claude/.credentials.json" ]; then
        DOCKER_CMD="$DOCKER_CMD -v \"$HOME/.claude/.credentials.json:/home/developer/.claude/.credentials.json\""
        echo "Credentials mounted read-write"
    fi
fi

# Mount /tmp/claude-UID for runtime state persistence
CLAUDE_TMP="/tmp/claude-$(id -u)"
mkdir -p "$CLAUDE_TMP"
DOCKER_CMD="$DOCKER_CMD -v \"$CLAUDE_TMP:/tmp/claude-$(id -u)\""

# Mount only Claude config (not all of ~/.config), read-only
if [ -d "$HOME/.config/claude" ]; then
    DOCKER_CMD="$DOCKER_CMD -v \"$HOME/.config/claude:/host-claude-config/.config/claude:ro\""
    echo "Mounting .config/claude (read-only)"
fi

# Mount Flutter SDK - try multiple common locations
FLUTTER_MOUNTED=false

# Check for snap Flutter installation (verify binary exists, not just empty snap dir)
if [ -f "/snap/flutter/current/bin/flutter" ]; then
    DOCKER_CMD="$DOCKER_CMD -v /snap/flutter:/host-flutter:ro"
    echo "Mounting Flutter from snap"
    FLUTTER_MOUNTED=true
elif [ -f "/opt/flutter/bin/flutter" ]; then
    DOCKER_CMD="$DOCKER_CMD -v /opt/flutter:/host-flutter:ro"
    echo "Mounting Flutter from /opt"
    FLUTTER_MOUNTED=true
elif [ -d "$HOME/flutter" ]; then
    DOCKER_CMD="$DOCKER_CMD -v \"$HOME/flutter:/host-flutter:ro\""
    echo "Mounting Flutter from ~/flutter"
    FLUTTER_MOUNTED=true
elif [ -d "/usr/local/flutter" ]; then
    DOCKER_CMD="$DOCKER_CMD -v /usr/local/flutter:/host-flutter:ro"
    echo "Mounting Flutter from /usr/local"
    FLUTTER_MOUNTED=true
elif [ -n "$FLUTTER_ROOT" ] && [ -d "$FLUTTER_ROOT" ]; then
    DOCKER_CMD="$DOCKER_CMD -v \"$FLUTTER_ROOT:/host-flutter:ro\""
    echo "Mounting Flutter from FLUTTER_ROOT: $FLUTTER_ROOT"
    FLUTTER_MOUNTED=true
fi

if [ "$FLUTTER_MOUNTED" = false ]; then
    echo "Flutter SDK not found on host (optional)"
fi

if [ -d "$HOME/.pub-cache" ]; then
    DOCKER_CMD="$DOCKER_CMD -v \"$HOME/.pub-cache:/home/developer/.pub-cache\""
fi

# Mount source repos (read-only) so global skill/hook symlinks resolve correctly
DAIC_DIR="$HOME/source/drift/drift-further_daic"
if [ -d "$DAIC_DIR" ]; then
    DOCKER_CMD="$DOCKER_CMD -v \"$DAIC_DIR:$DAIC_DIR:ro\""
    DOCKER_CMD="$DOCKER_CMD -e DAIC_SOURCE_DIR=\"$DAIC_DIR\""
    echo "Mounting DAIC source (read-only, for hooks/skills symlinks)"
fi

MATHPOL_DIR="$HOME/source/drift/drift-further_mathpolitics"
if [ -d "$MATHPOL_DIR" ]; then
    DOCKER_CMD="$DOCKER_CMD -v \"$MATHPOL_DIR:$MATHPOL_DIR:ro\""
    echo "Mounting MathPolitics source (read-only, for skills symlinks)"
fi

# Mount per-project packages file if provided
if [ -n "${PROJECT_PACKAGES_FILE:-}" ] && [ -f "$PROJECT_PACKAGES_FILE" ]; then
    DOCKER_CMD="$DOCKER_CMD -v $PROJECT_PACKAGES_FILE:/tmp/assist-project-packages.txt:ro"
fi

# Pass API key from .claude.json if available
if [ -f "$HOME/.claude.json" ]; then
    API_KEY=$(grep -o '"apiKey":"[^"]*' "$HOME/.claude.json" 2>/dev/null | cut -d'"' -f4)
    if [ -n "$API_KEY" ]; then
        DOCKER_CMD="$DOCKER_CMD -e ANTHROPIC_API_KEY=\"$API_KEY\""
        echo "API key passed from .claude.json"
    elif [ -n "$ANTHROPIC_API_KEY" ]; then
        DOCKER_CMD="$DOCKER_CMD -e ANTHROPIC_API_KEY=\"$ANTHROPIC_API_KEY\""
        echo "API key passed from environment"
    fi
else
    DOCKER_CMD="$DOCKER_CMD -e ANTHROPIC_API_KEY=\"$ANTHROPIC_API_KEY\""
fi
DOCKER_CMD="$DOCKER_CMD -e DISPLAY=:99"
DOCKER_CMD="$DOCKER_CMD -e HOME=/home/developer"
DOCKER_CMD="$DOCKER_CMD -e CLAUDE_AUTO_UPDATE=$CLAUDE_AUTO_UPDATE"
# Only pass CLAUDE_CMD if explicitly set — otherwise let entrypoint detect native binary
if [ -n "${CLAUDE_CMD:-}" ]; then
    DOCKER_CMD="$DOCKER_CMD -e CLAUDE_CMD=\"$CLAUDE_CMD\""
fi
# Database host override: inside the container, "localhost" is the container itself.
# Point to the host machine so project .env files with MP_DB_HOST=localhost still work.
DOCKER_CMD="$DOCKER_CMD -e MP_DB_HOST=$GATEWAY_HOST"
# CLI proxy points at the host Assist server
DOCKER_CMD="$DOCKER_CMD -e ASSIST_PROXY_HOST=$GATEWAY_HOST"
# Don't hardcode Flutter path - let entrypoint.sh handle it based on what's actually mounted
# Include Claude's install dir so the native binary and its runtime are on PATH
DOCKER_CMD="$DOCKER_CMD -e PATH=\"/home/developer/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\""
DOCKER_CMD="$DOCKER_CMD --shm-size=2gb"
DOCKER_CMD="$DOCKER_CMD --memory=$MEMORY_LIMIT --cpus=$CPU_LIMIT --pids-limit=$PIDS_LIMIT"
DOCKER_CMD="$DOCKER_CMD --network $CLAUDE_NETWORK"
DOCKER_CMD="$DOCKER_CMD -w /workspace"
DOCKER_CMD="$DOCKER_CMD $IMAGE_NAME"

echo ""
echo "Starting Claude Code container..."
echo "---"

# Run the container with arguments passed to the entrypoint
# Store $@ in an array BEFORE eval so special chars (parens, etc.) aren't re-parsed
_EXTRA_ARGS=("$@")
eval $DOCKER_CMD '"${_EXTRA_ARGS[@]}"'
