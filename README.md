# Drift Assist

A phone-first remote for the Claude Code, codex and OpenCode sessions running in
tmux on your own machine. Free and self-hosted: it runs on the host, serves a
mobile web UI, streams each pane live, types into it, and can answer permission
prompts for you when you ask it to.

> ### ⚠ Read this before you install
>
> Assist is a **single-owner** tool: one shared secret, no accounts, no roles, no audit trail. Anyone holding the secret is the owner.
>
> It **runs arbitrary commands on your machine by design** — that is the product, and there is no sandbox.
>
> It is for **loopback or a LAN you own**. Never expose it to the public internet, behind a tunnel or otherwise.
>
> **[SECURITY.md](SECURITY.md) is the full posture**, including what arming Auto-Yes actually hands over. Read it once before your first install.

## Requirements

- Linux or macOS
- Python 3.10 or newer
- tmux 3.2 or newer
- The agent CLIs you want to drive: [Claude Code](https://claude.com/claude-code), codex, OpenCode

```bash
# Debian / Ubuntu
sudo apt install python3 python3-venv tmux
# Fedora
sudo dnf install python3 tmux
# macOS (Homebrew) — the system /usr/bin/python3 is too old
brew install python@3.12 tmux
```

Optional: `xclip`, `xdotool` and `zenity` on Linux (clipboard, key-send, native
folder picker), and `curl`. `jq` and `docker` are used only by the container
tooling under `docker/`, which is parked.

## Install

```bash
# <REPO-URL> is a placeholder: the public repository URL is not decided yet.
git clone <REPO-URL> ~/.local/share/drift-assist
cd ~/.local/share/drift-assist
./install.sh
```

The installer checks the prerequisites, creates a venv and installs the pinned,
hash-checked dependencies from `requirements.lock`, seeds `.env` from
`env.example` (never overwriting an existing one), records the install path in
`~/.config/drift-assist/config.env`, and symlinks `~/.local/bin/assist` →
`bin/assist`. It then offers, default no, to run Assist as a service. The
`assist` command re-execs itself under the project venv, so it works from any
shell whatever virtualenv is active.

## Reach it from your phone

```bash
assist start      # or: assist service install  (starts now, and at every login)
assist expose     # also listen on this host's LAN address, and allow it in .env
assist pair       # print the phone URL and a QR code; scan it with the phone
```

- `assist start` prints the local URL, `http://localhost:8089/`, which works on
  the host straight away.
- `assist expose` sets `ASSIST_BIND` to the address of the default route
  (choose another with `--ip`) and adds `http://<that-address>:8089` to
  `ASSIST_ALLOWED_ORIGINS` in `.env`. It restarts the server if it is
  running. It refuses a wildcard such as `0.0.0.0` and any public address.
  `assist expose --off` goes back to loopback only.
- `assist pair` opens a sign-in window for five minutes (`--minutes N`). The
  first device on a private network (Settings → Access → Allowed Networks) to
  load the URL is signed in, and the window closes behind it. The strip across
  the top of the UI stays lit while a window is open.

The login page says the same: *No device signed in yet? Run `assist pair` on
the host.*

### Other ways to sign in

- **The token.** `assist token` prints where it is, and prints the value too
  when run in a terminal. Paste it into the login page once; the browser keeps a
  cookie from then on.
- **Approval.** A new browser can press *Request device approval* on the login
  page, and any signed-in session approves or denies it.

### Keep it running

`assist service install` writes a systemd `--user` unit on Linux or a launchd
agent on macOS, and starts it. From then on `assist start`, `stop`, `restart`
and `status` go through the unit. On Linux it starts at login. To keep it
running while you are logged out, and to start it at boot, run
`sudo loginctl enable-linger $USER` once. `assist service status` and
`assist service uninstall` do what they say.

### Advanced: a reverse proxy instead

Use this if you want a hostname, or TLS. Leave `ASSIST_BIND` unset so Flask
stays on loopback, add every phone-facing origin to `.env`, then put nginx on
the LAN address:

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

The Caddy equivalent (Caddy passes WebSocket upgrades through by itself):

```caddy
http://<lan-ip>:8089 {
    reverse_proxy 127.0.0.1:8089 {
        header_up X-Real-IP {remote_host}
    }
}
```

## Upgrade

```bash
cd ~/.local/share/drift-assist
git pull
./install.sh       # re-installs the pinned dependencies; never overwrites .env
assist restart
```

`assist --version` prints the version, plus the commit in a git checkout. Put
it in any bug report. Releases are not tagged yet.

## Uninstall

```bash
assist service uninstall                # only if you installed the service
assist stop
rm ~/.local/bin/assist
rm -rf ~/.config/drift-assist
rm -rf ~/.config/claude-assist          # pre-rename config dir, if present
rm -rf ~/.local/state/drift-assist      # PID file and logs
rm -rf ~/.local/share/drift-assist      # or wherever you cloned
```

The checkout also holds your prompt history (`history.json`), favorites and
segments (`favorites.json`), `settings.json` and `auth_token`. Copy anything you
want to keep before deleting it. An install from before the move to
`~/.local/state` may also have left `/tmp/assist-server.{pid,log}`.

If you approved status-line setup during installation, the installer may also
have changed `statusLine` in `~/.claude/settings.json`. Restore the timestamped
`~/.claude/settings.json.bak.<timestamp>` it created, or remove that
`statusLine` entry manually, before deleting the checkout whose script it names.

## CLI

Once installed, `assist` manages everything:

| Command | What it does |
|---------|--------------|
| `assist start` | Start the server (through the service unit when one is installed); prints the URL |
| `assist stop` | Stop the server |
| `assist restart` | Restart the server |
| `assist status` | Server status + health check |
| `assist logs [N\|-f\|--follow]` | Tail last N lines (default 100), or follow with `-f`/`--follow` |
| `assist service install\|uninstall\|status` | Run Assist as a systemd `--user` unit (Linux) or launchd agent (macOS) |
| `assist expose [--ip ADDR] [--off]` | Also listen on this host's LAN address, allow its origin, restart if running |
| `assist pair [--minutes N] [--url URL]` | Open a short sign-in window; print the phone URL and a QR code |
| `assist token` | Print the token file path, and the token itself only to a terminal |
| `assist --version` | Print the version (and git commit in a checkout) |
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
| `assist studio [args]` | Execute the Studio CLI found on `PATH` — `sto` first, then `studio` — or fail if neither is installed |
| `assist help` | Full command reference |

The process commands delegate to `./assist-ctl`, or to the service unit once `assist service install` has run. The container verbs are parked with the rest of the container tooling and are left out of `assist help` until that ends. The container commands hit the running server's HTTP API (`/api/container/*`), so the server must be running for them to work.

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
| `ASSIST_OPENCODE_BIN` | OpenCode executable used by the Output reader | Auto-detected; see [docs/details.md](docs/details.md) |
| `ASSIST_CLI_DIR` | Working directory used when invoking `ASSIST_CLI_BIN` | `~` |
| `ASSIST_CLI_ALLOWED` | Comma-separated allowlist of subcommands (**empty = proxy disabled**) | (empty) |
| `ASSIST_DB_NAME` | PostgreSQL DB for session history | `claude_archives` |
| `ASSIST_DB_HOST` | PostgreSQL host for session history | `localhost` |
| `ASSIST_BIND` | One extra LAN address to listen on, beside loopback. Written by `assist expose`; a wildcard such as `0.0.0.0` is refused | (loopback only) |
| `ASSIST_PID_FILE` | Server PID file | `~/.local/state/drift-assist/assist.pid` (`$XDG_STATE_HOME`) |
| `ASSIST_LOG_FILE` | Server log read by `assist logs`; moved to `.1` at start once over `ASSIST_LOG_MAX_BYTES` (10 MB) | `~/.local/state/drift-assist/assist.log` |
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

**Adding a device without typing the token.** A browser that arrives with no token can ask to be let in: it raises an approval request that any already-logged-in session sees and approves or denies. The request path is the one thing not behind the auth gate — a device with no token is exactly who calls it — so it is fenced instead by a LAN allowlist, a cap on pending requests, a per-IP cooldown, and a secret claim that binds an approval to the browser that asked. To onboard the first device, `assist pair` on the host (or **More → Access → Open** in a signed-in browser) starts a time-boxed open-access window, and the strip across the top of the UI stays lit until it closes.

Only three things are exempt: `/login`, `/health` (a liveness probe whose exact body is `{"status":"ok"}`), and `/api/cli-proxy` — containers have no way to hold the token, so that endpoint is restricted to the container subnet at the proxy layer and remains fail-closed on its own `ASSIST_CLI_ALLOWED` allowlist. The CLI proxy is also currently stopped by the temporary execution park before any subprocess can run.

**Flask listens on `127.0.0.1`,** plus the one LAN address `assist expose` sets in `ASSIST_BIND`. A wildcard (`0.0.0.0`, `::`) is refused at start, and `assist expose` refuses a public address. A reverse proxy on the LAN address is the alternative; see [Reach it from your phone](#reach-it-from-your-phone).

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

Point `api_base` at your Studio and paste your personal token into `api_token`. Hosted Studio enforces bearer auth, and Assist sends the token as `Authorization: Bearer` on every server-side call.

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

(`web_base` is what your browser opens — an nginx name, a LAN address, or empty to derive it from `api_base`. `api_base` is server-to-server: loopback for a Studio on this machine, or your hosted Studio's URL.)

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

## Architecture

- **`serve.py`** — Flask + flask-sock app factory, registers blueprints, starts background threads
- **`routes/`** — 15 Flask blueprints: `access`, `automate`, `autoyes`, `commands`, `completion`, `container`, `drafts`, `git`, `input`, `poll`, `settings`, `static`, `studio`, `tabstate`, `terminal`; WebSocket `streaming` is registered separately
- **`shared/`** — `state` (mutable state), `tmux` (tmux helpers), `utils`, `auth`, `security`, `agent_identity`, `agent_model`, `drafts`, `execution_park`, `launch_provenance`, `park_activation`, `segments`, `studio_client`, `tab_state`
- **`js/`** — 21 ES6 frontend modules (no framework, no bundler)
- **`css/`** — 15 CSS modules, mobile-first with custom properties
- **`docker/`** — parameterized `Dockerfile`, `entrypoint.sh`, extension definitions (`extensions/*.json`), helper scripts
- **`assist-ctl`** — low-level start/stop/restart/status shell script (called by `assist`); `assist-ctl run` is the foreground mode the service unit uses
- **`bin/assist`** — high-level CLI installed to `~/.local/bin/assist`
- **`docs/`** — [details.md](docs/details.md): the OpenCode Output reader, launch provenance, the temporary execution park and the host CLI proxy
- **`tests/`** — the unittest regression suite. Run the same command documented in `CLAUDE.md`: `.venv/bin/python3 -m unittest discover -s tests -p 'test_*.py'`

## More detail

[docs/details.md](docs/details.md) covers the OpenCode Output reader, launch
provenance, the temporary execution park, and the host CLI proxy.
