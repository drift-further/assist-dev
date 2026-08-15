# Claude Assist

Phone-friendly web terminal interface for managing Claude Code tmux sessions. Single-user tool running on the host (not Docker) behind nginx at `assist.drift`.

## Architecture

```
serve.py              Flask app factory — registers blueprints, starts background threads
index.html            Single page — all panels are collapsible sections, no routing
js/                   ES6 modules loaded via <script> tags in dependency order (no bundler)
css/                  CSS modules — hand-rolled, no framework
routes/               Flask blueprints, one per feature domain, + a WebSocket handler
shared/               state.py (all mutable state), tmux.py (tmux/X11 helpers), utils.py
```

`js/` and `routes/` are both **many small modules**, one per feature domain — not a couple of large
files. Locate by domain rather than reading everything: `ls js/ routes/`.

<!-- File/module counts were removed 2026-08-01: every one had drifted (index.html was
     described as ~393 lines when it was 532; 13 js modules when there were 17; 8
     blueprints when there were 15). Run `ls` — don't restate counts that go stale. -->

## Auth is live — read this before touching the server

**Every endpoint requires a shared secret.** `auth_token` lives in the repo root (0600, gitignored,
auto-generated on first start). The browser logs in once at `/login` and holds an HMAC cookie;
scripts pass `X-Assist-Token` or `?token=`. **Exempt: `/login`, `/health`, `/api/cli-proxy`.**

**Any test script hitting the API must send the token or it gets 401** — that is the most likely
cause of a sudden "everything returns 401". Rotate by deleting `auth_token` and restarting.

Flask binds **127.0.0.1 only**; nginx listens on the LAN address and forwards to loopback, so no
client URL changed. **The nginx vhost lives in a separate infrastructure repo** — not here, so a
fresh clone of this repo will not reproduce LAN access on its own.

**No build step.** Frontend is plain ES6 + CSS custom properties. No npm, no bundler, no framework. This is deliberate — zero frontend dependencies.

**Testing is manual**: edit, restart, verify on phone via Playwright or browser. The one exception is `tests/test_cli_proxy.py` — `/api/cli-proxy` is the only unauthenticated endpoint that runs a host binary, so its argument handling is pinned by unit tests: `.venv/bin/python3 -m unittest tests.test_cli_proxy` from the repo root. Nothing else has automated coverage, and it is not worth adding unless the project grows significantly.

## Code Style

- **Python**: snake_case. Clean imports: stdlib, then third-party, then local. No formatter configured — consistent by convention.
- **JavaScript**: camelCase. No framework, no transpilation. ES6 modules loaded in dependency order.
- **CSS**: Custom properties for theming. Mobile-first. All hand-rolled.
- **Commits**: `feat(scope):`, `refactor:`, `fix:`, `cleanup:`, `docs:`

## Design Constraints

Dark terminal aesthetic. Mobile-first with touch-friendly buttons.

| Token | Value | Usage |
|-------|-------|-------|
| `--bg` | `#080c10` | Background |
| `--green` | `#00ff41` | Terminal text |
| `--cyan` | `#00d4ff` | UI accents |
| `--amber` | `#ff9500` | Interactive elements |
| `--red` | `#ff0040` | Destructive actions |
| `--purple` | `#bf5af2` | Secondary accent |

Font stack: JetBrains Mono, Fira Code, SF Mono (monospace).

## Configuration

Three-tier settings with deep-merge defaults in `shared/state.py`:

| File | Scope |
|------|-------|
| `settings.json` | Global server/terminal/UI settings |
| `project_settings.json` | Per-project overrides (autoyes, automate, triggers) |
| `container_config.json` | Container build/runtime config |

All three are gitignored (runtime data). Defaults live in `shared/state.py` as `DEFAULT_SETTINGS`, `DEFAULT_PROJECT_SETTINGS`, `DEFAULT_CONTAINER_CONFIG`.

## CLI

| Command | Purpose |
|---------|---------|
| `assist start` | Start the server in the background |
| `assist stop` | Stop the server |
| `assist restart` | Restart the server |
| `assist status` | Show server status |
| `assist logs [N\|-f\|--follow]` | Tail the last N log lines (default 100), or follow the log |
| `assist config` | Print resolved paths, ports, and environment settings |
| `assist doctor` | Check prerequisites and server health |
| `assist container status` | Show image details and running `claude-session-*` containers |
| `assist container build` | Build the container image and stream the log |
| `assist container config` | Print the container build configuration |
| `assist container extensions` | List registered extension bundles |
| `assist container kill <name>` | Kill a running `claude-session-*` container |
| `assist ls [--json] [--cwd]` | List sessions and panes |
| `assist view <session> [-n N] [--pane P]` | Capture a session pane |
| `assist send <session> [<text>\|--file F] [--enter] [--pane P] [--wait] [--timeout N] [--autoyes]` | Send text, optionally wait, and temporarily arm auto-yes |
| `assist wait <session> [--timeout N] [--pane P] [--autoyes]` | Wait for a pane to settle |
| `assist launch --session N [--cwd P] [--cols C] [--rows R] [--wait] [--timeout N]` | Create a bare shell; takes no command. Spawn an agent with `launch`, then `send` |
| `assist kill <session> [--pane P]` | Kill a tmux session |
| `assist autoyes <session> (--on\|--off\|--status) [--delay N]` | Persistently set or inspect auto-yes; enabled delays are clamped to 0.1–30 seconds |
| `assist studio [args]` | Delegate to the Studio operator command |
| `assist help` | Show the full command reference |

Every session verb supports `-h`/`--help`, with descriptions for each positional argument and flag. `--autoyes` on `send` or `wait` applies only during that one wait and restores the prior setting afterward; `assist autoyes` changes the persistent per-session setting, and its `--delay` is valid only with `--on`.

Successful `ls`, `view`, `send`, `wait`, and `launch` commands print a measured `next:` suggestion to stderr. `ls` uses the first printed row's session name and omits the hint when no rows exist. Set `ASSIST_NO_HINTS=1` to suppress hints; `assist ls --json` suppresses them automatically and keeps stdout as parseable JSON.

`assist send` adds a `callback:` line offering the wording that asks the receiving agent to report back to the sender's own pane when it is done or stuck — the alternative to polling with `assist wait`. The reply address is the caller's tmux session, overridable with `ASSIST_REPLY_TO`, and the line is skipped outside tmux or when a session sends to itself.

| Wait exit | State | Meaning |
|-----------|-------|---------|
| `0` | idle | Pane went quiet, no prompt |
| `10` | prompt | Quiet because it is asking something; summary printed |
| `75` | working | Still changing at deadline; not an error, re-run `assist wait` |

## Development Cycle

1. Edit files in this repo
2. Restart: `assist restart` (canonical) — wraps `assist-ctl` (PID file, health check, logs)
3. Verify on phone or via Playwright at `http://assist.drift`

Python changes (serve.py, routes/) require restart. HTML/JS/CSS are served directly but may be browser-cached.

## Deployment

- Runs on host, port 8089
- nginx reverse proxy at `assist.drift` with WebSocket upgrade headers (`Upgrade`, `Connection "upgrade"`, `proxy_read_timeout 86400`)
- No staging environment — always edit, restart, verify live

## Blueprint Pattern

New routes follow the existing pattern: one blueprint per feature domain, registered in
`serve.py:create_app()`. See `routes/` for the current set. WebSocket streaming is registered
separately via `register_streaming(sock)`.

## Key Behaviors

- **WebSocket terminal streaming**: flask-sock, captures tmux panes, streams to connected clients
- **Smart actions**: JS pattern detection for permission prompts, numbered options, sudo — surfaces one-tap mobile actions
- **Prompt segments**: a favorite given a handle becomes `[handle]`; `shared/segments.py` expands it server-side in `/type` (opt-in via an `expand` flag) while history keeps the token form
- **Auto-yes**: Background scanner with per-session countdown timers for auto-approving prompts
- **Automate**: Continuous mode — sends prompts, watches for done signals, relaunches

## Branch Strategy

Everything on `main`. Feature branches for bigger work if needed.
