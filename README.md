# Claude Assist

A web terminal interface for [Claude Code](https://claude.com/claude-code) tmux sessions — designed for phones and tablets. Serves a mobile-first UI on port 8089, routes typing into a chosen tmux target, streams terminal output over WebSocket, and exposes a container build/spawn system for ephemeral dev environments.

Primary use case: control a Claude Code session running on your dev box from a phone over the LAN.

## Prerequisites

Before installing, make sure you have the required tools:

**Required:**

```bash
# Python 3.11+
python3 --version

# tmux (Assist routes input into tmux sessions)
# Linux (Debian/Ubuntu)
sudo apt install tmux
# or macOS
brew install tmux

# claude CLI (launches Claude Code in sessions; default mode)
# Install from: https://claude.com/claude-code
```

**Optional (but recommended):**

**Linux (Debian/Ubuntu):**
```bash
sudo apt install xclip xdotool curl docker.io
```

**macOS:**
```bash
brew install xclip xdotool curl docker
```

For **docker**, you may need additional setup:
- **Linux**: `sudo usermod -aG docker $USER && newgrp docker`
- **macOS**: Install [Docker Desktop](https://www.docker.com/products/docker-desktop)

**Other Linux distributions:**
- **Fedora/RHEL**: `sudo dnf install tmux xclip xdotool curl docker`
- **Arch**: `sudo pacman -S tmux xclip xdotool curl docker`

## Quick install

```bash
gh repo clone drift-further/assist-dev ~/.local/share/claude-assist
cd ~/.local/share/claude-assist
./install.sh
```

The installer creates a venv, installs Python deps, seeds `.env` from `env.example`, records the install path in `~/.config/claude-assist/config.env`, and symlinks `~/.local/bin/assist` → `bin/assist` so you get a global `assist` command.

Then:

```bash
assist start                  # start the server
assist doctor                 # verify prerequisites
```

Open `http://localhost:8089` (or `http://<host-ip>:8089` from your phone).

## CLI

Once installed, `assist` manages everything:

| Command | What it does |
|---------|--------------|
| `assist start` | Start the server (PID tracked in `/tmp/assist-server.pid`) |
| `assist stop` | Stop the server |
| `assist restart` | Restart the server |
| `assist status` | Server status + health check |
| `assist logs [N\|-f]` | Tail last N lines (default 100), or follow with `-f` |
| `assist config` | Print resolved paths, ports, env |
| `assist doctor` | Check prereqs, venv, .env, server health |
| `assist container status` | Image info + running `claude-session-*` containers |
| `assist container build` | Build the container image, streaming the log live |
| `assist container config` | Print current container build config |
| `assist container extensions` | List registered extension bundles |
| `assist container kill <name>` | Kill a running `claude-session-*` container |
| `assist help` | Full command reference |

The process commands delegate to `./assist-ctl`. The container commands hit the running server's HTTP API (`/api/container/*`), so the server must be running for them to work.

## Configuration

All configuration is environment-variable based, via `.env` in the repo. See `env.example` for the full list. The most common ones:

| Variable | Purpose | Default |
|----------|---------|---------|
| `ASSIST_PORT` | Port to listen on | `8089` |
| `ASSIST_PROJECTS_DIR` | Root directory for project discovery | `~/projects` |
| `ASSIST_SKILLS_DIR` | Claude skills directory | `~/.claude/skills` |
| `ASSIST_SESSION_INIT_CMD` | Command run in new tmux sessions | (none) |
| `ASSIST_MOUNT_SCRIPT` | Path to `claude-direct-mount.sh` for Automate | (none — required for Automate) |
| `ASSIST_DB_NAME` | PostgreSQL DB for session history | `claude_archives` |
| `DISPLAY` | X11 display for clipboard/key-send | `:1` |

Changes to `.env` require `assist restart` to take effect.

## Optional: nginx reverse proxy

Expose Assist at a friendly hostname on your LAN:

```nginx
server {
    listen 80;
    server_name assist.drift;

    location / {
        proxy_pass http://127.0.0.1:8089;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }
}
```

The WebSocket upgrade headers are essential — without them, the terminal falls back to HTTP polling.

## Architecture

- **`serve.py`** — Flask + flask-sock app factory, registers blueprints, starts background threads
- **`routes/`** — Flask blueprints: `terminal`, `input`, `git`, `commands`, `autoyes`, `automate`, `container`, `settings`, `static`, `poll`, `streaming`
- **`shared/`** — global mutable state, tmux wrappers, utilities
- **`js/`** — 13 ES6 frontend modules (no framework, no bundler)
- **`css/`** — 10 CSS modules, mobile-first with custom properties
- **`docker/`** — parameterized `Dockerfile`, `entrypoint.sh`, extension definitions (`extensions/*.json`), helper scripts
- **`assist-ctl`** — low-level start/stop/restart/status shell script (called by `assist`)
- **`bin/assist`** — high-level CLI installed to `~/.local/bin/assist`

## Uninstall

```bash
assist stop                             # stop the server first
rm ~/.local/bin/assist
rm -rf ~/.config/claude-assist
rm -rf ~/.local/share/claude-assist     # or wherever you cloned
```

Runtime files in `/tmp/assist-server.{pid,log}` can also be removed.
