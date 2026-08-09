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
sudo apt install xclip xdotool curl docker.io zenity
```

**macOS:**
```bash
brew install curl docker
```
> macOS uses `pbcopy`/`pbpaste` (clipboard) and `osascript` (folder picker, paste fallback) — both built-in. `xclip` and `xdotool` are Linux-only and not needed on macOS.

For **docker**, you may need additional setup:
- **Linux**: `sudo usermod -aG docker $USER && newgrp docker`
- **macOS**: Install [Docker Desktop](https://www.docker.com/products/docker-desktop)

**Other Linux distributions:**
- **Fedora/RHEL**: `sudo dnf install tmux xclip xdotool curl docker zenity`
- **Arch**: `sudo pacman -S tmux xclip xdotool curl docker zenity`

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
| `ASSIST_CLI_BIN` | Host CLI exposed to containers via `/api/cli-proxy` | (none — proxy disabled) |
| `ASSIST_CLI_DIR` | Working directory used when invoking `ASSIST_CLI_BIN` | `~` |
| `ASSIST_CLI_ALLOWED` | Comma-separated allowlist of subcommands (**empty = proxy disabled**) | (empty) |
| `ASSIST_DB_NAME` | PostgreSQL DB for session history | `claude_archives` |
| `ASSIST_ALLOWED_ORIGINS` | Extra browser origins accepted by the CSRF check, comma-separated. **Set this on any install that is not the original dev box** — the built-in list in `shared/security.py` hardcodes that machine's hostname and LAN IP, so your phone's address is rejected on POSTs until you add it | (built-in list only) |
| `DISPLAY` | X11 display for clipboard/key-send | `:1` |

Changes to `.env` require `assist restart` to take effect.

## Access and authentication

Assist can start a process in any tmux pane it manages, and `/api/commands/run` executes an arbitrary command string **by design** — the commands are ones you wrote yourself in the commands panel. So the only thing that can protect it is the trust boundary, not input validation.

Two things enforce that:

**A shared secret gates every endpoint.** On first start the server generates one and writes it to `auth_token` in the install directory (mode `0600`, gitignored); it is also printed to the startup log. Open Assist, paste the token into the login page once, and the browser holds a cookie from then on — the raw token is never stored client-side, only an HMAC of it. For scripts, pass it as an `X-Assist-Token` header or a `?token=` query parameter.

```
$ cat auth_token
BduKDRwh…
```

To rotate: delete `auth_token` and restart. A new secret is generated and every issued cookie stops matching, because the HMAC key changed.

**Adding a device without typing the token.** A browser that arrives with no token can ask to be let in: it raises an approval request that any already-logged-in session sees and approves or denies. The request path is the one thing not behind the auth gate — a device with no token is exactly who calls it — so it is fenced instead by a LAN allowlist, a cap on pending requests, a per-IP cooldown, and a secret claim that binds an approval to the browser that asked. If you would rather onboard the first device the blunt way, **More → Access → Open** starts a time-boxed open-access window, and the strip across the top of the UI stays lit until it closes.

Only three things are exempt: `/login`, `/health` (a liveness probe carrying no data), and `/api/cli-proxy` — containers have no way to hold the token, so that endpoint is instead restricted to the container subnet at the proxy layer and remains fail-closed on its own `ASSIST_CLI_ALLOWED` allowlist.

**Flask binds `127.0.0.1` only.** nginx is the sole ingress. Historically LAN clients reached Flask directly on `<lan-ip>:8089` because the vhost answered only to the `assist.drift` name, which left every endpoint exposed to the whole network. nginx now listens on that same address and port and forwards to loopback, so **no client URL changes** — binding a specific IP in nginx does not collide with Flask's loopback bind.

If you self-host this, the equivalent server block is:

```nginx
server {
    listen <lan-ip>:8089;
    server_name _;
    client_max_body_size 2G;

    location = /api/cli-proxy {
        allow 172.16.0.0/12;   # container subnet only
        allow 127.0.0.1;
        deny all;
        proxy_pass http://127.0.0.1:8089;
    }

    location / {
        proxy_pass http://127.0.0.1:8089;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
    }
}
```

Run `serve.py --host 0.0.0.0` to go back to binding all interfaces — but that re-exposes every endpoint to the network, and is only sane if you have no proxy in front.

## Composing prompts

The composer has three typeaheads. All of them trigger at the start of a line or after
whitespace, so ordinary shell text is never intercepted.

| Type | Completes | Resolved by |
|------|-----------|-------------|
| `@path` | Files and folders under the session's working directory, drilling in one level at a time | The agent in the pane — Assist only enumerates candidates |
| `/name` | Skills from `~/.claude/skills` and installed plugins | The agent in the pane |
| `[handle]` | **Segments** — your own reusable blocks of prompt text | **Assist**, server-side, at send time |

### Segments

A segment is a favorite you have given a short handle. Typing `[sol-dist]` sends that
favorite's entire body, so a 600-word standing instruction costs ten characters of phone
screen and you can stack several in one prompt:

```
tighten the poll loop [sol-dist] [house-style]
```

**To make one:** star any prompt from history, open the **Favs** tab, tap ✎, and give it a
handle. A favorite *without* a handle keeps its original behaviour — tapping it drops its
full text into the composer. One *with* a handle appends `[handle] ` to whatever you are
already writing, because a segment is a block you add rather than a prompt you recall.

While you type, each recognised handle shows as a green block inside the composer, with the
expanded character count beside it. Tap a block to read that body; tap the count to see
exactly what the pane will receive. Handles that do not exist show amber and are sent
literally.

Expansion happens on the server, immediately before the tmux send:

- **Only handles that exist are substituted.** Everything else passes through untouched, so
  `ls [abc]*`, `arr[0]` and `[0-9]` survive intact. Write `\[handle]` to force a literal.
- **Segments can reference segments**, up to three levels deep, with a cycle guard.
- **History stores what you typed, not what was sent** — recalling the prompt brings back the
  compact token form.
- Only the composer expands. The saved-command buttons post to the same endpoint and keep
  sending shell text byte-for-byte.

Segments live in `favorites.json` alongside everything else you have starred; the handle is
just an extra field, and existing favorites gain one lazily the first time they are read.

## Connect to Studio

[Studio](https://studio.drift) is the design hub agents report into — specs, plans, blocking questions, tasks and QA gates. Assist works standalone; connecting it to a Studio adds an attention inbox you can answer from your phone, a badge on the ◇ button, and a project/effort chip on the active session.

> **Loopback only for now.** Point `api_base` at a Studio on this machine (`http://127.0.0.1:8090`). **Do not expose Studio to a network and point Assist at it yet:** Studio does not enforce its API token, so a remote Studio would answer anyone who can reach it, and the `api_token` below would give you no protection you could rely on. Remote/hosted Studio is supported once Studio ships bearer enforcement.

Connect from the phone: tap **◇ Studio** while disconnected and fill the sheet. Or set it directly in `settings.json`:

```json
{
  "studio": {
    "web_base": "https://studio.drift",
    "api_base": "http://127.0.0.1:8090",
    "api_token": ""
  }
}
```

(`web_base` is what your browser opens — an nginx name, a LAN address, or empty to derive it from `api_base`. `api_base` is server-to-server and stays on loopback.)

| Key | Purpose | Default |
|-----|---------|---------|
| `studio.web_base` | What the browser opens (the Studio SPA). Empty derives it from `api_base`. | (empty) |
| `studio.api_base` | Server-to-server API base. Assist's Flask process is the only thing that calls it. | `http://127.0.0.1:8090` |
| `studio.api_token` | Sent as `Authorization: Bearer`. Stored server-side only; `GET /api/settings` returns it as `***`. | (empty) |

The connection is the whole feature switch — there is no license check in Assist. The panel shows four states: **connected**, **unreachable** (Studio down; the last known queue is still shown), **unauthorized** (token rejected), **unconfigured** (no Studio set up).

Notes:
- All Studio traffic is server-proxied. The token never reaches the browser, and Studio needs no CORS changes.
- A slow or dead Studio never slows Assist: a background thread does the polling and `/poll` reads its snapshot.
- `settings.json` is written `0600` because it holds the token.
- Settings → **Reset All to Defaults** clears `studio.api_token` along with everything else.
- Anyone who can reach Assist's port can answer your Studio questions — the same trust boundary as every other Assist endpoint. Keep it on a trusted network.

## Host CLI proxy

Containers launched by Assist run on an isolated network with no LAN access, but they can reach the host on port 8089. This is used to expose a single host-side CLI tool inside the container without copying its dependencies in.

Two pieces:

1. **Host-side** (`.env`) — `ASSIST_CLI_BIN`, `ASSIST_CLI_DIR`, `ASSIST_CLI_ALLOWED`. The Flask server's `/api/cli-proxy` endpoint runs `ASSIST_CLI_BIN <args>` on the host and returns stdout/stderr/exit code. `ASSIST_CLI_ALLOWED` (comma-separated) restricts which first-arg subcommands are accepted; **leaving it empty disables the proxy (403)**.
2. **Container-side** (`container_config.json` → `cli_proxy`) — set `enabled: true` and `container_command: "<name>"`. The image build installs a thin bash wrapper at `/usr/local/bin/<name>` that POSTs to the proxy.

Example — exposing a host CLI called `mycli`:

```bash
# .env
ASSIST_CLI_BIN=/home/me/bin/mycli
ASSIST_CLI_DIR=/home/me/source/mycli
ASSIST_CLI_ALLOWED=status,build,deploy
```

```json
// container_config.json
{
  "cli_proxy": { "enabled": true, "container_command": "mycli" }
}
```

Then `assist restart` and rebuild the image (`assist container build` or the Container panel). Inside any newly-launched container, `mycli status` runs against the host binary.

The wrapper accepts `-f <path>` to base64-upload a file from the container — the host writes it to a temp dir and replaces the arg with the resolved path before invoking the CLI.

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
- **`routes/`** — 15 Flask blueprints, one per feature domain: `access`, `automate`, `autoyes`, `commands`, `completion`, `container`, `git`, `input`, `poll`, `settings`, `static`, `streaming`, `studio`, `tabstate`, `terminal`
- **`shared/`** — `state` (all mutable state), `tmux` (tmux/X11 helpers), `utils`, `auth` (shared secret, device approval), `security` (origin allowlist), `agent_identity` (what is actually running in a pane), `segments` ([handle] expansion), `studio_client`, `tab_state`
- **`js/`** — 18 ES6 frontend modules (no framework, no bundler)
- **`css/`** — 15 CSS modules, mobile-first with custom properties
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
