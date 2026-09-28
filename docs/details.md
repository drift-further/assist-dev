# Drift Assist — details

Material moved out of the README's first screens: how the OpenCode reader
works, the launch-provenance store, the temporary execution park, and the host
CLI proxy.

## Platform notes

Linux is the primary deployment platform. Interactive tmux input and launch
provenance read process identity through Linux `/proc` or macOS `libproc`;
macOS clipboard helpers use `pbcopy`/`pbpaste`. The v16 activation/drain tooling
still requires Linux `/proc`.

The receipted park activation controller is `assist activate-park-v16 [--resume]`.
It is left out of `assist help`.

## OpenCode Output reader

OpenCode panes also offer **Output**: choose the conversation shown in the pane
to read wrapped messages, reasoning and tool details with normal browser scrolling.
**Latest** follows new output; **Terminal** returns to the interactive TUI, and
detected prompts return there automatically. Input always goes to the pane, so
choose again in Output after switching conversations inside OpenCode.

In OpenCode's **Terminal** view, drag in either direction to pan a capture
larger than the phone. Swipe at its top or bottom edge, or use **▲ / ▼**, to
page through the app's transcript. The TUI stays live while you read;
scrolling up holds your position, and **Latest** returns to the newest output.

The reader uses the host's `opencode session list` and `opencode export --pure`
(verified with 1.18.18), refreshing snapshots while visible. It needs no plugin
or listening OpenCode server. Selection lasts for the browser's current pane
generation. Remote/custom data stores use Terminal; a local `attach` must share
the host's OpenCode store. The picker searches 200 recent sessions in the pane's
exact folder; message/detail limits are disclosed in the reader.

The OpenCode reader uses the first executable file found in this order:
`ASSIST_OPENCODE_BIN`, `opencode` on the server's `PATH`,
`~/.local/bin/opencode`, `~/.opencode/bin/opencode`,
`/opt/homebrew/bin/opencode`, then `/usr/local/bin/opencode`.
Set `ASSIST_OPENCODE_BIN` to an absolute path for a custom installation;
`~/` is also accepted. Missing paths, directories and non-executable files
are skipped. These fallbacks work when a service has a minimal `PATH`.


## Launch provenance

On an ordinary startup, Assist automatically creates a missing
`.assist-launch-provenance-v1/` store using the same empty-epoch operation as
the explicit initializer and writes
`.assist-launch-provenance-v1-initialization.json` beside it. Startup never
re-initializes an existing path: an existing but invalid store still fails
closed. The park-handoff startup path does not run this initializer.


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

