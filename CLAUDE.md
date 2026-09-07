# Claude Assist

> **This repo is PUBLIC on GitHub.** Stage your work and ask before committing — commits
> here are visible to anyone and use the configured Git author. Never `git push`. Never mention Claude,
> AI, or co-authorship in a commit message.


Phone-friendly web terminal interface for managing Claude Code tmux sessions. Single-user tool running on the host (not Docker) behind an nginx reverse proxy. The LAN hostname is per-install and deliberately not in the repo — read it from `ASSIST_ALLOWED_ORIGINS` in `.env`.

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
scripts pass **`X-Assist-Token`** — the header is the only accepted carrier for the raw secret;
`?token=` was removed (URL credentials leak into proxy logs, history and `Referer`).
**Exempt: `/login`, `/health`, `/api/cli-proxy`.** `/health` returns exactly
`{"status":"ok"}`. The CLI proxy is currently stopped by the execution park
before its inner subnet and allowlist gates can run. The posture doc is
`SECURITY.md`.

**Any test script hitting the API must send the token or it gets 401** — that is the most likely
cause of a sudden "everything returns 401". Rotate by deleting `auth_token` and restarting.

Flask binds **127.0.0.1 only**; nginx listens on the LAN address and forwards to loopback, so no
client URL changed. **The nginx vhost lives in a separate infrastructure repo** — not here, so a
fresh clone of this repo will not reproduce LAN access on its own.

**No build step.** Frontend is plain ES6 + CSS custom properties. No npm, no bundler, no framework. This is deliberate — zero frontend dependencies.

Run the full unittest regression suite from the repo root:

```bash
.venv/bin/python3 -m unittest discover -s tests -p 'test_*.py'
```

| Module | Why it has tests |
|---|---|
| `test_cli_proxy` | the outer parked 409 and the retained inner gates that can run a host binary only when the intent is enabled in a future release |
| `test_auth_token_transport` | which carriers may present the shared secret (header/cookie yes, query string no) |
| `test_autoyes_detection` | prompt fixtures in BOTH directions — must-fire and must-not-fire. A detector made too strict fails silently |
| `test_autoyes_posture` | the shipped Auto-Yes defaults, and the delay clamp that keeps a hand-edited settings file from removing the countdown |
| `test_sudo_removed` | the stored sudo password is gone and must stay gone |
| `test_vault_grammar` | `$` vault tokens stay outside the server segment grammar; secret `/type` sends stay exact and unrecorded |
| `test_vault_client_contract` | client-only guards couple vault resolution to secret sending and keep vault chip preview local |
| `test_favorites` | the star on a history row: add, remove, keep-a-segment. The add path broke one-directionally when a refactor dropped an import, and the phone showed nothing |
| `test_autoyes_downgrade_menu` | the codex veto: while a model-downgrade menu (or the luna model) is on screen, a codex pane is not auto-answered, because a bare Enter there takes the downgrade. Fixtures in both directions, so the veto cannot silently widen to every codex pane |

One test is **not** in that command, because it needs a browser and a running server:
`tests/playwright_vault_wire.js` proves the secret vault on the wire rather than in the source —
it asserts over recorded network traffic that a resolved `[$handle]` reaches exactly one request,
that the request is `POST /type` with `secret: true`, and that no draft, history or
`/segments/expand` call ever carries the value. Point it at a throwaway instance, never the live
one, and note it blocks service workers, so it cannot see a `sw.js` regression — that is what the
index/cache parity guard in `test_vault_client_contract` is for.

```bash
NODE_PATH=/path/to/node_modules \
ASSIST_URL=http://127.0.0.1:8099 ASSIST_TOKEN="$(cat auth_token)" \
  node tests/playwright_vault_wire.js
```

Use focused module invocations while iterating, then run the discover command before handoff.

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

`bin/assist` re-execs itself under `.venv/bin/python3`, because the shebang resolves against the caller's PATH and the CLI imports flask via `routes.autoyes`. It therefore runs from any shell, with any venv active or none. With no `.venv` present it prints an `./install.sh` hint instead of an import traceback.

**The re-exec decision is `sys.prefix == .venv`, and nothing else.** `ASSIST_VENV_REEXEC` is only an anti-loop backstop and it is **stamped with the pid** — `os.execv` preserves the pid, so a stamp written in this exec chain matches `os.getpid()` while a value inherited from another process does not. Checking the bare *presence* of that variable is what broke: the server carries it in its own environ, every tmux pane it spawns inherited it, and the wrapper then skipped the re-exec while not in the venv — so every verb died with `missing dependency 'flask'` against a perfectly healthy venv.

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
| `assist container build` | Request an image build; currently parked with HTTP 409 |
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
| `assist autoyes --global (--on\|--off\|--status) [--delay N]` | Set or inspect the all-sessions switch |
| `assist studio [args]` | Execute a separate `studio` CLI on `PATH`; fail clearly when none is installed |
| `assist help` | Show the full command reference |

Every session verb supports `-h`/`--help`, with descriptions for each positional argument and flag. `--autoyes` on `send` or `wait` applies only during that one wait and restores the prior setting afterward; `assist autoyes` changes the persistent per-session setting, and its `--delay` is valid only with `--on`.

`assist autoyes --global` sets the all-sessions switch (`autoyes.all_sessions`, also in Settings → Auto-Yes). While it is on, every **agent** pane — claude, codex, opencode, cursor, gemini — in every session is armed at `autoyes.default_delay`, including sessions created later, and that one delay applies to all (`/autoyes/set-delay` returns 409). Plain shell panes stay manual unless that session was armed by hand, which keeps `apt`, ssh host-key and stray `(y/n)` prompts out of scope. A session toggled off while the switch is on records `autoyes.global_opt_out` and stays off. `assist autoyes <session> --status` reports which of the two is in play: `on (global, …)`, `on (set here, …)`, `off (opted out of global)`, or `off (no agent pane)`.

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
3. Verify on phone or via Playwright at the LAN hostname (in `.env`, `ASSIST_ALLOWED_ORIGINS`) — not `127.0.0.1`, which is out of scope for the device-approval and open-access checks

Python changes (serve.py, routes/) require restart. HTML/JS/CSS are served directly but may be browser-cached.

## Deployment

- Runs on host, port 8089
- nginx reverse proxy on the LAN hostname with WebSocket upgrade headers (`Upgrade`, `Connection "upgrade"`, `proxy_read_timeout 86400`)
- No staging environment — always edit, restart, verify live

## Blueprint Pattern

New routes follow the existing pattern: one blueprint per feature domain, registered in
`serve.py:create_app()`. See `routes/` for the current set. WebSocket streaming is registered
separately via `register_streaming(sock)`.

## Key Behaviors

- **WebSocket terminal streaming**: flask-sock, captures tmux panes, streams to connected clients
- **Smart actions**: JS pattern detection for permission prompts and numbered options — surfaces one-tap mobile actions. Mirrors the server matchers in `routes/autoyes.py`; keep the two in step
- **Prompt segments**: a favorite given a handle becomes `[handle]`; `shared/segments.py` expands it server-side in `/type` (opt-in via an `expand` flag) while history keeps the token form
- **Auto-yes**: Background scanner with per-session countdown timers for auto-approving prompts. An `autoyes.all_sessions` switch arms every agent pane at once — resolved at scan time (runtime map → project settings → switch), so new sessions are covered with no backfill — with a per-session opt-out. Codex panes are vetoed rather than answered while the luna model or one of codex's model-downgrade menus is on screen: a bare Enter there re-tiers the pane
- **Automate**: Continuous mode exists, but its start/relaunch/clear/resend/trust/answer execution intents are temporarily parked while container host wiring migrates

### Temporary execution park

The exact denied intents are `automate_start`, `automate_hard_relaunch`,
`automate_soft_clear`, `automate_soft_resend`, `automate_trust_answer`,
`automate_auto_answer`, `configured_cli_proxy`, and `configured_image_build`.
Everything else remains allowed, including saved commands, `/api/git/run`,
project-venv creation, `/api/restart`, terminal run-init, launch/duplicate with
an init command, and the native folder picker. There is no un-park verb, and
`ExecutionPark.perform()` intentionally ignores `Phase`.

Denied HTTP calls return 409. For `automate_start`, the exact body is:

```json
{
  "ok": false,
  "error": "container_launch_parked",
  "reason": "Container launch automation is temporarily parked while host wiring migrates.",
  "intent": "automate_start"
}
```

### Launch provenance initialization

On ordinary non-handoff startup, `serve.py` calls the same empty-epoch
initializer as `python -m shared.launch_provenance initialize --expect-empty`
when `.assist-launch-provenance-v1/` is absent, and writes
`.assist-launch-provenance-v1-initialization.json`. It never runs when the store
path already exists, so corrupt state is not repaired and runtime reads still
fail closed. Keep the `--park-handoff-fd` path free of this initialization.

## Branch Strategy

Everything on `main`. Feature branches for bigger work if needed.
