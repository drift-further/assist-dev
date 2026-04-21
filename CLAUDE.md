# Claude Assist

Phone-friendly web terminal interface for managing Claude Code tmux sessions. Single-user tool running on the host (not Docker) behind nginx at `assist.drift`.

## Architecture

```
serve.py              Flask app factory — registers blueprints, starts background threads
index.html            Single page (~393 lines) — all panels are collapsible sections, no routing
js/                   13 ES6 modules loaded via <script> tags in dependency order (no bundler)
css/                  10 CSS modules — hand-rolled, no framework
routes/               Flask blueprints, one per feature domain (8 blueprints + 1 WebSocket handler)
shared/               state.py (all mutable state), tmux.py (tmux/X11 helpers), utils.py
```

**No build step.** Frontend is plain ES6 + CSS custom properties. No npm, no bundler, no framework. This is deliberate — zero frontend dependencies.

**No test suite.** Testing is manual: edit, restart, verify on phone via Playwright or browser. Not worth adding unless the project grows significantly.

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

New routes follow the existing pattern: one blueprint per feature domain, registered in `serve.py:create_app()`. Current blueprints: static, input, terminal, git, commands, autoyes, automate, container, poll, settings. WebSocket streaming is registered separately via `register_streaming(sock)`.

## Key Behaviors

- **WebSocket terminal streaming**: flask-sock, captures tmux panes, streams to connected clients
- **Smart actions**: JS pattern detection for permission prompts, numbered options, sudo — surfaces one-tap mobile actions
- **Auto-yes**: Background scanner with per-session countdown timers for auto-approving prompts
- **Automate**: Continuous mode — sends prompts, watches for done signals, relaunches

## Branch Strategy

Everything on `main`. Feature branches for bigger work if needed.
