# Container Management for Claude Assist

## Overview

Assist becomes the canonical home for the containerized Claude environment. The container definition (Dockerfile, scripts, entrypoint) moves from KAREN's `docker-containers/claude-flutter-full/` into `assist-dev/docker/`. A new Container panel in the UI provides image building, package management, extension management, and running container oversight.

## Goals

- Non-technical users can set up and manage safe Claude containers through the UI
- Package management (pip, apt) at both global (baked into image) and per-project (installed at startup) levels
- Generic extension system for SDKs/tools (Flutter, Playwright, Go, etc.) without hardcoding any specific SDK
- Configurable base versions (Node, Python, Claude CLI)
- Network and resource controls with clear security defaults

## Architecture

### File Structure

```
assist-dev/
  docker/                       # Container definition
    Dockerfile                  # Parameterized with build args + extension support
    entrypoint.sh               # Container init (venv, packages, Claude config)
    scripts/                    # Helper scripts
      karen-proxy.sh            # HTTP proxy to host Assist server
      send-discord.sh           # Message bridge: container -> Discord
      check-discord.sh          # Message bridge: Discord -> container
      link-agents.sh            # Symlink project agents
      discord-poll-hook.sh      # Claude hook for Discord polling
      claude-statusline.sh      # Status line showing context/model
    extensions/                 # Common pre-built extension definitions
      flutter.json
      playwright.json
      go.json
      rust.json
      java.json
  routes/
    container.py                # NEW blueprint — build, status, packages, extensions
  js/
    container.js                # NEW — Container panel UI logic
  css/
    container.css               # NEW — Container panel styles
  container_config.json         # Global container settings (created on first use)
  extensions.json               # Registered extensions (created on first use)
```

### Three Layers of Customization

1. **Base image** — Node + Python + Claude CLI + core tools (ripgrep, git, jq, psql client). Version-configurable via build args. Rarely rebuilt.
2. **Extensions** — User-defined SDK/tool bundles. Each is an archive path + install commands. Baked into image on rebuild. Toggle on/off.
3. **Packages** — Global pip/apt packages (baked in on rebuild) + per-project pip packages (installed at container startup by entrypoint).

## Configuration

### container_config.json

```json
{
  "base": {
    "node_version": "20",
    "python_version": "3.12",
    "claude_version": "latest"
  },
  "resources": {
    "memory": "16g",
    "cpus": "4",
    "pids_limit": 512
  },
  "network": {
    "bind_address": "127.0.0.1",
    "allow_lan": false,
    "allow_ports": [5432, 8089],
    "gateway_host": "10.0.0.101"
  },
  "packages": {
    "pip": ["psycopg2-binary", "httpx", "beautifulsoup4"],
    "system": ["postgresql-client", "ripgrep"]
  },
  "image": {
    "name": "claude-assist-container",
    "built_at": null,
    "build_hash": null
  }
}
```

- `base.*` — Build args passed to Dockerfile. Changing triggers rebuild.
- `resources.*` — Applied at runtime via `docker run` flags. No rebuild needed.
- `network.bind_address` — `127.0.0.1` (local only) or `0.0.0.0` (open to network). Applied at runtime.
- `network.allow_lan` — When false, iptables rules block RFC1918 ranges except allowed ports on gateway_host.
- `packages.pip` — Global pip packages baked into image. Changing triggers rebuild.
- `packages.system` — Global apt packages baked into image. Changing triggers rebuild.
- `image.*` — Metadata updated after successful builds.

### extensions.json

```json
[
  {
    "id": "flutter",
    "name": "Flutter SDK",
    "builtin": true,
    "archive": "/opt/flutter.tar.xz",
    "install": [
      "tar -xf /tmp/ext-archive -C /opt",
      "export PATH=/opt/flutter/bin:$PATH",
      "flutter precache --web"
    ],
    "env": {"FLUTTER_ROOT": "/opt/flutter"},
    "path_add": "/opt/flutter/bin",
    "enabled": false
  },
  {
    "id": "playwright",
    "name": "Playwright Browsers",
    "builtin": true,
    "archive": null,
    "install": [
      "npx playwright install chromium firefox",
      "npx playwright install-deps"
    ],
    "env": {"PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright"},
    "path_add": null,
    "enabled": true
  },
  {
    "id": "go",
    "name": "Go SDK",
    "builtin": true,
    "archive": null,
    "install": [
      "curl -fsSL https://go.dev/dl/go${GO_VERSION:-1.22.0}.linux-amd64.tar.gz | tar -C /usr/local -xz"
    ],
    "env": {"GOPATH": "/home/developer/go"},
    "path_add": "/usr/local/go/bin:/home/developer/go/bin",
    "enabled": false
  },
  {
    "id": "rust",
    "name": "Rust Toolchain",
    "builtin": true,
    "archive": null,
    "install": [
      "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
    ],
    "env": {},
    "path_add": "/home/developer/.cargo/bin",
    "enabled": false
  },
  {
    "id": "java",
    "name": "OpenJDK",
    "builtin": true,
    "archive": null,
    "install": [
      "apt-get update && apt-get install -y --no-install-recommends openjdk-17-jdk-headless && rm -rf /var/lib/apt/lists/*"
    ],
    "env": {"JAVA_HOME": "/usr/lib/jvm/java-17-openjdk-amd64"},
    "path_add": null,
    "enabled": false
  }
]
```

- `builtin` — true for pre-shipped extensions (show badge in UI, cannot be deleted)
- `archive` — path to local tarball/zip, or null if install commands handle download
- `install` — shell commands run during `docker build` in order
- `env` — environment variables set in the container
- `path_add` — appended to PATH in the container
- `enabled` — toggle; changing triggers rebuild

### Per-Project Packages (in project_settings.json)

Extends the existing per-project settings with a packages section:

```json
{
  "drift-further_KAREN": {
    "autoyes": { "delay": 5 },
    "automate": { "timeout": 10 },
    "triggers": { "done_signals": ["~:)Investigate done(:~"] },
    "packages": {
      "pip": ["flask", "psycopg2-binary"]
    }
  }
}
```

Per-project pip packages are installed at container startup by entrypoint.sh (not baked into the image). This avoids rebuilds for project-specific dependencies.

## Container Panel UI

Accessed from the + menu (replaces CMD). Amber-themed to match the Automate panel family.

### Status Section (always visible at top)

- Image name, build date, size
- Base versions: Node / Python / Claude
- Active containers: count + list with session name, uptime, kill button

### Packages Section (collapsible)

- **Global pip** — tag list with add/remove. Changes flag "rebuild needed".
- **Project pip** — tag list for current project. Stored in project_settings.json, installed at startup. No rebuild.
- **System apt** — tag list with add/remove. Changes flag "rebuild needed".

### Extensions Section (collapsible)

- List with toggle switches
- "built-in" badge for pre-shipped extensions
- "Add Custom" button opens form: name, archive path, install commands (multiline), env vars, PATH additions
- Edit/delete for custom extensions
- Pending changes show amber "Rebuild to apply" banner

### Build Section (collapsible)

- Node version selector (18 / 20 / 22)
- Python version selector (3.10 / 3.11 / 3.12 / 3.13)
- Claude version input (default "latest")
- Network: bind address toggle (Local Only / Open), LAN access toggle, allowed ports
- Resources: memory, CPUs, PID limit steppers
- **Rebuild button** — prominent green, shows streaming build log in output area
- Build log area: scrollable, auto-scroll, shows docker build output in real time

## API Endpoints

All on the `container_bp` blueprint, registered in serve.py.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/container/status` | Image info + running containers |
| GET | `/api/container/config` | Current container_config.json |
| PATCH | `/api/container/config` | Update config (auto-save) |
| POST | `/api/container/build` | Trigger image rebuild (streams via WebSocket) |
| GET | `/api/container/extensions` | List all extensions |
| POST | `/api/container/extensions` | Add new custom extension |
| PATCH | `/api/container/extensions/<id>` | Update/toggle extension |
| DELETE | `/api/container/extensions/<id>` | Remove custom extension (builtin cannot be deleted) |
| POST | `/api/container/kill/<name>` | Kill a running claude-session container |

## Build Flow

1. User modifies settings/packages/extensions in Container panel
2. Changes auto-save to `container_config.json` / `extensions.json`
3. Panel shows amber "Rebuild to apply" banner for build-time changes
4. User clicks Rebuild:
   a. Backend generates build command with `--build-arg` flags from config
   b. Backend generates an `extensions-install.sh` script from enabled extensions
   c. `docker build` runs with the `assist-dev/docker/` build context
   d. Build output streams to UI via WebSocket
   e. On success: updates `image.built_at`, clears "rebuild needed" flag
   f. On failure: shows error in build log, image unchanged

## Runtime Flow (Automate Launch)

1. `claude-direct-mount.sh` reads `container_config.json` for resource limits and network rules
2. Container starts from built image with runtime flags (memory, cpus, network)
3. `entrypoint.sh` runs:
   a. Standard init (Claude config, venv, display)
   b. Reads mounted per-project packages file → `pip install` if present
   c. Reads workspace `requirements.txt` → `pip install` if present (existing behavior)
   d. Launches Claude with prompt
4. Automate monitor tracks container as before (done signals, idle, relaunch)

## Dockerfile Parameterization

Key build args:
```dockerfile
ARG NODE_VERSION=20
ARG PYTHON_VERSION=3.12
ARG CLAUDE_VERSION=latest

FROM node:${NODE_VERSION}-bookworm

# Python
RUN apt-get update && apt-get install -y python${PYTHON_VERSION} python${PYTHON_VERSION}-venv ...

# Claude CLI
RUN curl -fsSL https://claude.ai/install.sh | bash

# Global packages (from container_config.json, injected as build args)
ARG PIP_PACKAGES=""
ARG SYSTEM_PACKAGES=""
RUN if [ -n "$SYSTEM_PACKAGES" ]; then apt-get install -y $SYSTEM_PACKAGES; fi
RUN if [ -n "$PIP_PACKAGES" ]; then pip install $PIP_PACKAGES; fi

# Extensions (generated script from enabled extensions)
COPY extensions-install.sh /tmp/
RUN chmod +x /tmp/extensions-install.sh && /tmp/extensions-install.sh
```

## Security Defaults

- `bind_address`: `127.0.0.1` (local only by default)
- `allow_lan`: false (blocks RFC1918 ranges via iptables)
- No Docker socket mounted (prevents host manipulation)
- Sudo with dangerous commands blocked (rm, dd, reboot, kill, iptables, docker, etc.)
- Workspace mounted read-write, credentials mounted read-only
- Resource limits enforced (memory, CPUs, PIDs)

## Menu Change

The + menu removes the **CMD** button and adds **Container** in its place. The saved commands feature (`routes/commands.py`, `js/commands.js`) remains in the codebase but is no longer accessible from the menu. The commands panel HTML, JS, and CSS can be cleaned up in a follow-up.

## Files Modified/Created Summary

| File | Action | Purpose |
|------|--------|---------|
| `docker/Dockerfile` | Create | Parameterized base image + extension support |
| `docker/entrypoint.sh` | Create | Container init (from KAREN, + project packages) |
| `docker/scripts/*.sh` | Create | 6 helper scripts (from KAREN) |
| `docker/extensions/*.json` | Create | 5 built-in extension definitions |
| `container_config.json` | Create | Global container settings |
| `extensions.json` | Create | Active extension registry |
| `routes/container.py` | Create | Container management API |
| `js/container.js` | Create | Container panel UI |
| `css/container.css` | Create | Container panel styles |
| `index.html` | Modify | Add Container panel HTML, remove CMD from menu |
| `serve.py` | Modify | Register container blueprint |
| `shared/state.py` | Modify | Add container config load/save functions |
| `claude-direct-mount.sh` | Modify | Read from container_config.json |
| `project_settings.json` | Extend | Add packages.pip per project |
