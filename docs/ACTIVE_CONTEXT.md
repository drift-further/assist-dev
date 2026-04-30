# Active Context

**Last Updated**: 2026-04-29 (Phase 1 shipped)
**Current Task**: Fix terminal artifacts + redesign idle-window collapse (pill + bottom sheet)
**Branch**: main
**Mode**: Phase 1 committed; awaiting live-tmux verification before Phase 2

---

## Current Focus

### What We Are Doing
Two-part work on the phone terminal UI:
1. **Phase 1 — Artifacts (SHIPPED, awaiting live verification):** three commits on main:
   - `29a0a98 fix(streaming): pop ws_last_content cache before adding client` (cache-pop now inside ws_lock; bundled WIP `_force_repaint` SIGWINCH helper)
   - `c8bcb44 fix(terminal): cancel pending render on tab switch; add _doRender target guard` (selectTab clears `_renderTimer`/`_pendingRender`; `?v=4` cache-buster; bundled WIP Fit-menu)
   - `4481a27 fix(tmux): capture current screen only on alt-screen` (`capture_pane` queries `#{alternate_on}`, uses `-S 0` for TUIs)
2. **Phase 2 — Idle collapse UX (PENDING):** approved pill + bottom-sheet design replacing inline `.stale-tab-group` chevron. Tasks 4-7 in plan, not yet started.

Spec at `docs/superpowers/specs/2026-04-29-terminal-artifacts-and-idle-collapse-design.md`.
Plan at `docs/superpowers/plans/2026-04-29-terminal-artifacts-and-idle-collapse.md`.

### Key Decisions Made This Session
- Cache-pop moved inside `ws_lock`, before `ws_clients.append` (closes server race)
- `selectTab` cancels `_renderTimer` + nulls `_pendingRender`; `_doRender` has target guard (closes client race)
- `capture_pane` queries `#{alternate_on}`: `-S 0` for TUIs, `-S -{lines}` for shells
- Phase 2 stale tabs will collapse to small `[zZ N]` pill at right of strip; tap opens 220ms bottom sheet
- Tap-row → session active + back to strip + sheet auto-closes (180ms); not auto-pinned
- Pinned never tuck; active never tucks; running auto-promotes; team-lead+agents tuck as a unit
- Commits: simple subject lines, no AI/Claude references, no Co-Authored-By trailer (per user instruction)
- Phase 1 commits bundled unrelated WIP that lived in same files (_force_repaint, Fit-menu) — by user direction

### Blockers / Open Questions
- [ ] User to verify Phase 1 fixes against a live tmux session before Phase 2 starts
- [ ] Prior task (CLI-proxy container rebuild) still has `.env` paste pending; remaining WIP in `shared/state.py`, `README.md`, `docker/*`, `env.example`, `routes/container.py`, `routes/poll.py`, `docker/scripts/cli-proxy.sh`

---

## Quick References

### Critical Files for Current Task
| File | Why It Matters |
|------|----------------|
| `docker/scripts/cli-proxy.sh` | Generic wrapper; POSTs to /api/cli-proxy |
| `docker/Dockerfile` | `ARG CLI_PROXY_NAME` controls in-container install name |
| `docker/claude-mount.sh` | Passes the build arg on `-b`; injects `ASSIST_PROXY_HOST` env |
| `shared/state.py` | `DEFAULT_CONTAINER_CONFIG` now includes `cli_proxy` |
| `routes/container.py` | `_run_build` forwards `CLI_PROXY_NAME` build-arg |
| `routes/poll.py:361` | Pre-existing generic `/api/cli-proxy` host endpoint |
| `container_config.json` | Seeded `cli_proxy.enabled=true, container_command="karen"` |

---

## Recent Context

### Session History
| Date | Focus | Outcome |
|------|-------|---------|
| 2026-04-29 | Diagnose karen-cli unavailable in container | Root cause: helper scripts and `.env`/`container_config.json` config never made it from KAREN repo into assist-dev's initial commit |
| 2026-04-29 | Implement generic CLI proxy | Wrapper, build arg, config seeded; .env left for user |
| 2026-04-29 | Rebuild image with latest Claude | In progress |

---

## Do Not Forget

- [ ] User must manually update `.env` with karen ASSIST_CLI_* values (hook-blocked)
- [ ] After build completes, verify `karen` symlink exists in image and a fresh container can call it
