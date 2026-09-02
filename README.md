# Claude Assist

A web terminal interface for [Claude Code](https://claude.com/claude-code) tmux sessions — designed for phones and tablets. Serves a mobile-first UI on port 8089, routes typing into a chosen tmux target, streams terminal output over WebSocket, and exposes a container build/spawn system for ephemeral dev environments.

Primary use case: control a Claude Code session running on your dev box from a phone over the LAN.

> ### ⚠ Read this before you install
>
> Assist is a **single-owner** tool: one shared secret, no accounts, no roles, no audit trail. Anyone holding the secret is the owner.
>
> It **runs arbitrary commands on your machine by design** — that is the product, and there is no sandbox.
>
> It is for **loopback or a LAN you own**. Never expose it to the public internet, behind a tunnel or otherwise.
>
> **[SECURITY.md](SECURITY.md) is the full posture**, including what arming Auto-Yes actually hands over. Read it once before your first install.

## Prerequisites

Assist currently supports **Linux only**. Some clipboard helpers also support
macOS (`pbcopy`/`pbpaste`), but the v16 process-identity and launch-provenance
path requires Linux `/proc`.

Before installing, make sure you have the required tools:

**Required:**

```bash
# Python 3.11+
python3 --version

# tmux 3.2 or newer, plus jq
# Linux (Debian/Ubuntu)
sudo apt install tmux jq

# claude CLI (launches Claude Code in sessions; default mode)
# Install from: https://claude.com/claude-code
```

**Optional (but recommended):**

**Linux (Debian/Ubuntu):**
```bash
sudo apt install xclip xdotool curl docker.io zenity
```

For **docker**, you may need additional setup:
- **Linux**: `sudo usermod -aG docker $USER && newgrp docker`

**Other Linux distributions:**
- **Fedora/RHEL**: `sudo dnf install tmux jq xclip xdotool curl docker zenity`
- **Arch**: `sudo pacman -S tmux jq xclip xdotool curl docker zenity`

## Quick install

```bash
git clone https://github.com/drift-further/assist-dev ~/.local/share/assist-dev
cd ~/.local/share/assist-dev
./install.sh
```

The installer creates a venv, installs Python deps, seeds `.env` from `env.example`, records the install path in `~/.config/claude-assist/config.env`, and symlinks `~/.local/bin/assist` → `bin/assist` so you get a global `assist` command. That command re-execs itself under the project venv, so it works from any shell whatever virtualenv happens to be active.

Then:

```bash
assist start                  # start the server
assist doctor                 # verify prerequisites
```

On an ordinary startup, Assist automatically creates a missing
`.assist-launch-provenance-v1/` store using the same empty-epoch operation as
the explicit initializer and writes
`.assist-launch-provenance-v1-initialization.json` beside it. Startup never
re-initializes an existing path: an existing but invalid store still fails
closed. The park-handoff startup path does not run this initializer.

Open `http://localhost:8089` on the host. Flask listens on loopback, so a phone
cannot connect directly to `http://<host-ip>:8089`. For LAN access, add the
phone-facing origin to `.env`, then put nginx on the LAN address:

```bash
ASSIST_ALLOWED_ORIGINS=http://<lan-ip>:8089
```

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

Use the exact scheme, hostname or address, and port your phone opens in
`ASSIST_ALLOWED_ORIGINS`. Restart after changing `.env`. The WebSocket upgrade
headers are required for live terminal streaming.

## CLI

Once installed, `assist` manages everything:

| Command | What it does |
|---------|--------------|
| `assist start` | Start the server (PID tracked in `/tmp/assist-server.pid`) |
| `assist stop` | Stop the server |
| `assist restart` | Restart the server |
| `assist activate-park-v16 [--resume]` | Run or resume the receipted new-first park activation controller |
| `assist status` | Server status + health check |
| `assist logs [N\|-f\|--follow]` | Tail last N lines (default 100), or follow with `-f`/`--follow` |
| `assist config` | Print resolved paths, ports, env |
| `assist doctor` | Check prereqs, venv, .env, server health |
| `assist container status` | Image info + running `claude-session-*` containers |
| `assist container build` | Request a container image build (temporarily parked; returns 409) |
| `assist container config` | Print current container build config |
| `assist container extensions` | List registered extension bundles |
| `assist container kill <name>` | Kill a running `claude-session-*` container |
| `assist ls [--json] [--cwd]` | List sessions and panes, optionally with working directories |
| `assist view <session> [-n N] [--pane P]` | Capture a session pane |
| `assist send <session> [<text>\|--file F] [--enter] [--pane P] [--wait] [--timeout N] [--autoyes]` | Send text to a session pane, optionally waiting for it to settle |
| `assist wait <session> [--timeout N] [--pane P] [--autoyes]` | Wait for a session pane to settle |
| `assist launch --session N [--cwd P] [--cols C] [--rows R] [--wait] [--timeout N]` | Create a bare shell; takes no command (spawn an agent with `launch`, then `send`) |
| `assist kill <session> [--pane P]` | Kill a tmux session |
| `assist autoyes <session> (--on\|--off\|--status) [--delay N]` | Persistently set or inspect auto-yes; enabled delays are clamped to 0.1–30 seconds |
| `assist autoyes --global (--on\|--off\|--status) [--delay N]` | Set or inspect the all-sessions switch |
| `assist studio [args]` | Execute a separate `studio` CLI found on `PATH`, or fail if none is installed |
| `assist help` | Full command reference |

The process commands delegate to `./assist-ctl`. The container commands hit the running server's HTTP API (`/api/container/*`), so the server must be running for them to work.

Each session verb (`ls`, `view`, `send`, `wait`, `launch`, `kill`, and `autoyes`) supports `-h`/`--help`; its generated help describes every argument and flag. `--autoyes` on `send` or `wait` is a temporary window scoped to that one wait and restores the prior state afterward. `assist autoyes` changes the persistent per-session setting instead; `--delay` is valid only with `--on`.

Auto-Yes answers permission prompts for you after a countdown, which means an agent stops asking before doing things you would otherwise have been asked about. It ships **off**, with a 5-second countdown, and [SECURITY.md](SECURITY.md#auto-yes-what-arming-it-means) sets out what arming it hands over — including that it decides by pattern-matching pane text an agent may not have authored. Every start prints the current posture to the log, so an armed install is never a silent one.

`assist autoyes --global` sets the all-sessions switch (also in Settings → Auto-Yes → All Sessions). While it is on, every **agent** pane — claude, codex, opencode, cursor, gemini — in every session is armed at the one global delay, including sessions created later; per-session delays do not apply and `/autoyes/set-delay` returns 409. Plain shell panes stay manual unless that session was armed by hand, so `apt`, ssh host-key and stray `(y/n)` prompts are out of scope. Turning one session off while the switch is on records an opt-out that survives a restart. `--status` on a session says which rule applied: `on (global, …)`, `on (set here, …)`, `off (opted out of global)`, or `off (no agent pane)`.

After a successful `ls`, `view`, `send`, `wait`, or `launch`, the CLI prints a measured `next:` suggestion to stderr, leaving stdout safe for captures and pipelines. `ls` uses the first printed row's session name and omits the hint when there are no rows. Set `ASSIST_NO_HINTS=1` to suppress hints for any command; `assist ls --json` suppresses them automatically so its stdout remains parseable JSON.

`assist send` prints a second stderr line, `callback:`, carrying the sentence that asks the receiving agent to message you back when it finishes or hits a question — so the sender can be told rather than poll `assist wait`. It is a suggestion; the sender decides whether to include it. The reply address is the caller's own tmux session (`ASSIST_REPLY_TO` overrides it), and the line is omitted when there is no such address — outside tmux nothing can be replied to — or when a session is sending to itself.

Session wait commands use these exit codes:

| Code | State | Meaning |
|------|-------|---------|
| `0` | idle | The pane went quiet with no prompt |
| `10` | prompt | The pane is quiet because it is asking something; a summary is printed |
| `75` | working | The pane is still changing at the deadline; this is not an error, so re-run `assist wait` |

## Temporary execution park

While container host wiring migrates, Assist refuses exactly these execution
intents:

- Automate start, hard relaunch, soft clear, soft resend, trust answer, and
  auto-answer (`automate_start`, `automate_hard_relaunch`,
  `automate_soft_clear`, `automate_soft_resend`, `automate_trust_answer`, and
  `automate_auto_answer`).
- The configured host CLI proxy (`configured_cli_proxy`, `/api/cli-proxy`).
- Configured container image builds (`configured_image_build`,
  `/api/container/build`).

Saved commands, `/api/git/run`, project-venv creation, `/api/restart`, run-init,
launch or duplicate with an init command, and the native folder picker remain
available. There is no un-park API or CLI verb; changing the park phase does not
enable a denied intent. For example, a refused Automate start returns HTTP 409
with this exact body (the `intent` value identifies the refused operation):

```json
{
  "ok": false,
  "error": "container_launch_parked",
  "reason": "Container launch automation is temporarily parked while host wiring migrates.",
  "intent": "automate_start"
}
```

## Configuration

All configuration is environment-variable based, via `.env` in the repo. See `env.example` for the full list. The most common ones:

| Variable | Purpose | Default |
|----------|---------|---------|
| `ASSIST_PORT` | Port to listen on | `8089` |
| `ASSIST_PROJECTS_DIR` | Root directory for project discovery | `~/projects` |
| `ASSIST_SKILLS_DIR` | Claude skills directory | `~/.claude/skills` |
| `ASSIST_SESSION_INIT_CMD` | Command run in new tmux sessions | (none) |
| `ASSIST_REPLY_TO` | Reply address used by the `assist send` callback hint | (the caller's own tmux session) |
| `ASSIST_MOUNT_SCRIPT` | Container launch script used by Automate | `docker/claude-mount.sh` when that file exists; otherwise none |
| `ASSIST_CLI_BIN` | Host CLI exposed to containers via `/api/cli-proxy` | (none — proxy disabled) |
| `ASSIST_CLI_DIR` | Working directory used when invoking `ASSIST_CLI_BIN` | `~` |
| `ASSIST_CLI_ALLOWED` | Comma-separated allowlist of subcommands (**empty = proxy disabled**) | (empty) |
| `ASSIST_DB_NAME` | PostgreSQL DB for session history | `claude_archives` |
| `ASSIST_DB_HOST` | PostgreSQL host for session history | `localhost` |
| `ASSIST_PID_FILE` | Server PID file | `/tmp/assist-server.pid` |
| `ASSIST_LOG_FILE` | Server log read by `assist logs` | `/tmp/assist-server.log` |
| `ASSIST_CONTROL_DIR` | Park-activation control and receipt directory | `/tmp/assist-park-v16` |
| `ASSIST_AUTH_TOKEN_PATH` | Shared-secret file | `<assist-home>/auth_token` |
| `ASSIST_ALLOWED_ORIGINS` | Browser origins accepted by the CSRF check, comma-separated. **Required on any install reached from more than localhost** — `shared/security.py` ships loopback only, so list your hostname and the LAN address your phone uses or every POST from them 403s while GETs still work | (loopback only) |
| `DISPLAY` | X11 display for clipboard helpers | `:0` |

Changes to `.env` require `assist restart` to take effect.

## Access and authentication

Assist can start a process in any tmux pane it manages, and `/api/commands/run`
executes arbitrary command strings **by design**, including saved commands an
agent may have written into `.assist-commands.json`. So the only thing that can
protect it is the trust boundary, not input validation.
**[SECURITY.md](SECURITY.md) states that boundary in full**; this section is the
mechanism.

Two things enforce it:

**A shared secret gates every endpoint.** On first start the server generates one and writes it to `auth_token` in the install directory (mode `0600`, gitignored). A direct interactive start prints the token value and its file path; redirected startup output prints only the path, so the value is not copied into the server log. Open Assist, paste the token into the login page once, and the browser holds a cookie from then on — the raw token is never stored client-side, only an HMAC of it. Scripts send it in the **`X-Assist-Token` header** — the only accepted carrier for the raw secret. A `?token=` query parameter is *not* accepted: a credential in a URL ends up in proxy access logs, browser history and outbound `Referer` headers, and it would only ever have authenticated the HTML document and none of its stylesheets or scripts.

```
$ cat auth_token
BduKDRwh…
```

To rotate: delete `auth_token` and restart. A new secret is generated and every issued cookie stops matching, because the HMAC key changed.

**Adding a device without typing the token.** A browser that arrives with no token can ask to be let in: it raises an approval request that any already-logged-in session sees and approves or denies. The request path is the one thing not behind the auth gate — a device with no token is exactly who calls it — so it is fenced instead by a LAN allowlist, a cap on pending requests, a per-IP cooldown, and a secret claim that binds an approval to the browser that asked. If you would rather onboard the first device the blunt way, **More → Access → Open** starts a time-boxed open-access window, and the strip across the top of the UI stays lit until it closes.

Only three things are exempt: `/login`, `/health` (a liveness probe whose exact body is `{"status":"ok"}`), and `/api/cli-proxy` — containers have no way to hold the token, so that endpoint is restricted to the container subnet at the proxy layer and remains fail-closed on its own `ASSIST_CLI_ALLOWED` allowlist. The CLI proxy is also currently stopped by the temporary execution park before any subprocess can run.

**Flask binds `127.0.0.1` only.** nginx is the LAN ingress. The Quick install
section includes the supported reverse-proxy block and required origin setting.

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

[Studio](https://driftstudio.dev) is the design hub agents report into — specs, plans, blocking questions, tasks and QA gates. Assist works standalone; connecting it to a Studio adds an attention inbox you can answer from your phone, a badge on the ◇ button, and a project/effort chip on the active session.

> **Loopback only for now.** Point `api_base` at a Studio on this machine (`http://127.0.0.1:8090`). **Do not expose Studio to a network and point Assist at it yet:** Studio does not enforce its API token, so a remote Studio would answer anyone who can reach it, and the `api_token` below would give you no protection you could rely on. Remote/hosted Studio is supported once Studio ships bearer enforcement.

Connect from the phone: tap **◇ Studio** while disconnected and fill the sheet. Or set it directly in `settings.json`:

```json
{
  "studio": {
    "web_base": "https://studio.example.com",
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

The proxy is currently in the temporary execution park: every request returns
the 409 body documented above, with `"intent":"configured_cli_proxy"`, before a
host subprocess starts. The configuration and inner subnet/allowlist gates
below remain in place for a future release that enables the intent; there is no
runtime un-park verb.

Two pieces:

1. **Host-side** (`.env`) — `ASSIST_CLI_BIN`, `ASSIST_CLI_DIR`, `ASSIST_CLI_ALLOWED`. The Flask server's `/api/cli-proxy` endpoint runs `ASSIST_CLI_BIN <args>` on the host and returns stdout/stderr/exit code. `ASSIST_CLI_ALLOWED` (comma-separated) restricts which first-arg subcommands are accepted; **leaving it empty disables the proxy (403)**.
2. **Container-side** (`container_config.json` → `cli_proxy`) — set `enabled: true` and `container_command: "<name>"`. The image build installs a thin bash wrapper at `/usr/local/bin/<name>` that POSTs to the proxy.

Example — exposing a host CLI called `mycli`:

```bash
# .env
ASSIST_CLI_BIN=/opt/mycli/bin/mycli
ASSIST_CLI_DIR=/srv/mycli
ASSIST_CLI_ALLOWED=status,build,deploy
```

```json
// container_config.json
{
  "cli_proxy": { "enabled": true, "container_command": "mycli" }
}
```

Once a future release enables both parked intents, restart and rebuild the image. Inside a newly launched container, `mycli status` then runs against the host binary.

The wrapper accepts `-f <path>` to base64-upload a file from the container — the host writes it to a temp dir and replaces the arg with the resolved path before invoking the CLI. The temp dir is removed whether or not the call succeeds.

A `--timeout N` in the forwarded args sets how long the host waits, plus 30s of slack. It must be a non-negative integer — anything else is a 400 rather than a silent fallback — and it is capped at 600s, so a proxied call cannot hold a host subprocess open indefinitely.

## Architecture

- **`serve.py`** — Flask + flask-sock app factory, registers blueprints, starts background threads
- **`routes/`** — 15 Flask blueprints: `access`, `automate`, `autoyes`, `commands`, `completion`, `container`, `drafts`, `git`, `input`, `poll`, `settings`, `static`, `studio`, `tabstate`, `terminal`; WebSocket `streaming` is registered separately
- **`shared/`** — `state` (mutable state), `tmux` (tmux helpers), `utils`, `auth`, `security`, `agent_identity`, `agent_model`, `drafts`, `execution_park`, `launch_provenance`, `park_activation`, `segments`, `studio_client`, `tab_state`
- **`js/`** — 21 ES6 frontend modules (no framework, no bundler)
- **`css/`** — 15 CSS modules, mobile-first with custom properties
- **`docker/`** — parameterized `Dockerfile`, `entrypoint.sh`, extension definitions (`extensions/*.json`), helper scripts
- **`assist-ctl`** — low-level start/stop/restart/status shell script (called by `assist`)
- **`bin/assist`** — high-level CLI installed to `~/.local/bin/assist`
- **`tests/`** — the unittest regression suite. Run the same command documented in `CLAUDE.md`: `.venv/bin/python3 -m unittest discover -s tests -p 'test_*.py'`

## Uninstall

```bash
assist stop                             # stop the server first
rm ~/.local/bin/assist
rm -rf ~/.config/claude-assist
rm -rf ~/.local/share/assist-dev        # or wherever you cloned
```

Runtime files in `/tmp/assist-server.{pid,log}` can also be removed.

If you approved status-line setup during installation, the installer may also
have changed `statusLine` in `~/.claude/settings.json`. Restore the timestamped
`~/.claude/settings.json.bak.<timestamp>` it created, or remove that
`statusLine` entry manually, before deleting the checkout whose script it names.
