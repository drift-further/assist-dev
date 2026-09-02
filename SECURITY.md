# Security posture

Read this before you install Assist. It is not a disclaimer. It is the design.

## What Assist is

**Assist is a single-owner tool.** One person owns the machine, and everyone who
can reach Assist is that person. There are no accounts, no roles, no per-user
data and no audit trail. There is one shared secret, and holding it means being
the owner. If two people hold it, they are the same user as far as Assist is
concerned, and neither can see what the other did.

**Assist runs arbitrary commands on your machine, by design.** That is the
product. It types into live tmux panes, starts processes, and `/api/commands/run`
executes command strings, including commands an agent wrote into
`.assist-commands.json`. There is no sandbox and no allowlist, because a terminal
with an allowlist is not a terminal. Anything that can reach an authenticated
Assist endpoint can do anything your user account can do — read your files, your
SSH keys, your cloud credentials, your source code; write anywhere you can write;
and reach anything on your network that you can.

**Assist must never be exposed to the public internet.** Not behind
authentication, not behind a VPN you also expose, not "just for a minute". Flask
binds `127.0.0.1` only, and the supported deployments are exactly two:

* **Loopback** — you use it on the machine it runs on.
* **Trusted LAN** — a reverse proxy on your own network forwards to loopback,
  and every device that can route to it is a device you own.

If you port-forward it, put it on a public cloud host, or expose it through a
tunnel, you have published a remote shell on your machine with a single
password and no rate limit. Nothing in this repository will save you.

Assist has not been reviewed for multi-tenant use and is not built for it.
Do not deploy it for a team.

## The trust boundary, in one paragraph

The only thing protecting Assist is who can reach the port and who holds the
secret. Input validation is not a control here, because the input is meant to be
arbitrary. So the security of your install is entirely the security of your
network and of `auth_token`.

## The shared secret

`auth_token` is generated on first start, written beside the code with mode
`0600`, and gitignored. A direct start attached to a terminal prints the value
and its file path. Redirected startup output prints only the path, so the raw
value is not copied into the server log. It is presented one of two ways and no
others:

* the browser exchanges it once at `/login` for a cookie holding an HMAC of it —
  the raw secret is never stored client-side;
* scripts and the `assist` CLI send it in the **`X-Assist-Token` header**.

The browser login field uses `autocomplete="current-password"`, so a browser
password manager may offer to store this shared secret.

**A `?token=` query parameter is not accepted.** A credential in a URL gets
written down by things Assist does not control: the reverse proxy logs the full
request line, the browser keeps it in history and in any bookmark or shared
link, and it leaves in the `Referer` header of the next outbound request. Use
the header. (For a headless browser, set it on the browser context — a token in
the URL would authenticate the HTML document but none of its stylesheets or
scripts anyway.)

The login cookie lasts ten years and is never revalidated. That is deliberate —
a phone must not be asked to re-authenticate mid-session — and it means
**revocation is deleting `auth_token` and restarting**. Every issued cookie stops
matching, because the HMAC key changed. Do that if a device is lost, if the
token was pasted somewhere it should not have been, or if you are not sure.

Three endpoints are exempt from the gate: `/login`, `/health` (a liveness probe
whose complete response body is `{"status":"ok"}`), and `/api/cli-proxy`.
Containers cannot hold the token, so the proxy is fenced at the proxy layer to
the container subnet and stays fail-closed on its own `ASSIST_CLI_ALLOWED`
allowlist — empty means disabled. It is also currently stopped by the temporary
execution park before any subprocess runs. The device-approval pair is also
reachable without a token, because a device with no token is exactly who calls
it; it is fenced instead by a LAN allowlist, a cap on pending requests, a per-IP
cooldown, and a single-use claim that binds an approval to the browser that
asked.

## Temporary execution park

While container host wiring migrates, Assist refuses only these eight intents:
`automate_start`, `automate_hard_relaunch`, `automate_soft_clear`,
`automate_soft_resend`, `automate_trust_answer`, `automate_auto_answer`,
`configured_cli_proxy`, and `configured_image_build`. That parks Automate's
launch/relaunch/clear/resend/trust/answer execution, `/api/cli-proxy`, and
`/api/container/build`. Saved commands, fixed git operations, project-venv
creation, configured restart, terminal init/duplicate/run-init, and the native
folder picker remain available.

There is no API or CLI operation that un-parks an intent, and changing the
published park phase does not change this deny-list. A refused HTTP operation
returns 409; for Automate start the exact body is:

```json
{
  "ok": false,
  "error": "container_launch_parked",
  "reason": "Container launch automation is temporarily parked while host wiring migrates.",
  "intent": "automate_start"
}
```

## Launch provenance on a fresh install

Ordinary startup automatically initializes a missing
`.assist-launch-provenance-v1/` store as an empty epoch and writes the
`.assist-launch-provenance-v1-initialization.json` receipt. Initialization runs
only when the store path does not exist. An existing corrupt or incomplete
store is never repaired or replaced at startup; readers continue to fail
closed. The park-handoff startup path does not initialize provenance.

## Auto-Yes: what arming it means

Auto-Yes watches tmux panes for permission prompts and answers them for you
after a countdown. When it is armed, **an agent no longer has to ask you before
doing something you would have been asked about** — running a command, writing a
file, deleting one. That is the point of the feature and it is a real transfer
of authority, not a UI convenience.

Two things follow, and you should decide about both before turning it on.

**It answers by pattern-matching pane text, and pane text is not trustworthy.**
An agent that prints a diff, `cat`s a README, or renders something it fetched
has put text on the screen that Assist did not author. The detectors are
anchored so that ordinary output does not trip them — a bare `(y/n/a)` must end
its line, option rows must have the shape of a rendered button row, numbered
menus must sit under a live footer with no internal divider — and
`tests/test_autoyes_detection.py` pins both directions. Those anchors are narrow
enough for real code and prose to pass under them, and they are not proof
against text written specifically to trip them. If you run agents over content
from sources you do not control, that is the risk you are accepting.

**The all-sessions switch is much larger than it looks.** `autoyes.all_sessions`
arms *every agent pane on the host* — claude, codex, opencode, cursor, gemini —
at one delay, including sessions created after you flipped it. Plain shell panes
stay manual, which is what keeps `apt`, ssh host-key and stray `(y/n)` prompts
out of scope, and a session turned off by hand records an opt-out that survives
a restart.

Shipped defaults, which a fresh clone gets because `settings.json` is gitignored:

| Setting | Ships as | Means |
|---|---|---|
| `autoyes.all_sessions` | `"off"` | nothing is armed until you arm it, per session |
| `autoyes.default_delay` | `5` | five seconds to see the prompt and cancel |

The countdown is the only chance you get to stop an answer, so it is clamped to
at least 100 ms even if a settings file says `0`. A sub-second delay is
effectively no window at all. Every start prints the current posture to the log
next to the auth-token file notice, so an armed install can never be a silent
one:

```
[assist] auto-yes: ALL SESSIONS ARMED at 0.1s — every agent pane on this host …
```

Check it with `assist autoyes --global --status`, or per session with
`assist autoyes <session> --status`, which tells you *which* rule applied.

## Secret vault: plaintext in this browser

Assist can store values for `[$handle]` tokens in the browser's `localStorage`
under `assist.vault.v1`. **Those values are plaintext.** They are not encoded,
encrypted or sent to the server for storage. When a token resolves, the browser
sends the resulting value only as `/type` content with `secret: true`, which
turns off trimming, first-word case fixing, segment expansion and history.

This buys one specific boundary: someone who steals only an Assist auth cookie
on another device cannot retrieve the vault and does not get a stored path to
root. It does **not** protect against someone holding the unlocked browser or
reading that browser profile. That device already controls the terminal.

Script execution in Assist's origin is a separate boundary, not another way of
saying “holding the browser.” Injected or compromised same-origin JavaScript can
read the plaintext vault without possessing the device, then send or disclose
those values using the browser's authenticated session. Preventing untrusted
script from running in this origin is therefore part of protecting the vault.

`localStorage` is scoped to the exact origin: scheme, host and port. Moving an
install from `http://assist.example.lan` to `https://assist.example.lan` creates a different
store. The existing vault does not migrate and will appear empty on HTTPS; any
needed entries must be entered again on the new origin, then forgotten from the
old origin while it is still reachable.

Encryption waits on trusted TLS for `assist.example.lan`. The phone reaches Assist
over plain HTTP, which is not a secure context; browsers therefore do not make
`crypto.subtle` available there. Service workers have the same secure-context
requirement (apart from browsers' loopback exception), so the offline shell is
also inert on a phone using the current plain-HTTP LAN origin. Once TLS lands,
the intended design is a WebAuthn-gated vault whose key stays behind the
platform authenticator. Assist will not hand-roll AES or PBKDF2 in JavaScript to
route around the secure-context requirement.

Use the per-entry **Forget** controls or **Forget all vault entries** in Settings
to remove browser-stored values.

The `[$sudo]` quick key is offered from prompt shape alone. A pane can fake that
shape, so look at the prompt before you tap it.

## What is deliberately not here

**No server-stored sudo password.** Assist used to keep one in `sudo_pw.dat` and
type it into a pane on request. It is gone. A borrowed auth cookie must not be
enough to ask the server for root without ever seeing the password. You can type
a password directly — the composer detects the live prompt and sends it
byte-for-byte without history — or opt into the browser-local plaintext vault
described above. The server has no endpoint that stores or returns either value.

If you have an old install, delete the leftover file:

```bash
rm -f sudo_pw.dat
```

**No secret redaction.** Terminal output is streamed verbatim. If a command
prints a key, Assist shows it and stores it in the session capture.

**No rate limiting on `/login`.** The token is 32 bytes from `secrets`; the
control is its entropy, not attempt throttling. This is another reason the port
must not be public.

## Hardening checklist

1. Keep Flask on `127.0.0.1` and put your reverse proxy on a LAN address.
2. Set `ASSIST_ALLOWED_ORIGINS` to the hostnames you actually use. It ships
   loopback-only, so a LAN install that skips this gets 403 on every POST.
3. Narrow `access.open_networks` from the shipped all-private-ranges default to
   the one range your devices are on.
4. Leave `ASSIST_CLI_ALLOWED` empty unless you use containers. Empty disables
   `/api/cli-proxy` entirely.
5. Leave Auto-Yes off until you have watched it answer a prompt you were
   expecting, and leave the all-sessions switch off unless you actually want
   every pane on the machine covered.

## Reporting

This is a single-maintainer hobby project with no security SLA. Open an issue
for anything that is not itself sensitive; for something that is, contact the
maintainer privately rather than filing publicly. Expect a best-effort response
and no coordinated-disclosure process.
