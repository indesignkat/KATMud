# Web/phone dashboard — design

Date: 2026-06-21

## Purpose

A phone-reachable dashboard so the user can, while away from the PC (e.g.
waiting on a doordash order), see at a glance whether a wiz has sent a
tell (top priority — needed to respond to a "bot check" before it causes
trouble), check pending reminders, glance at mud output to confirm a
character is actually fighting, and optionally kick off a bot command
(including starting a `Hunt` in a different area). HP bars, MIP data, and
other richer visuals are explicitly out of scope for this first pass.

Today the system is desktop-only: `katmud.pyw` spawns one detached
Tkinter process per character (see `README.md`); there is no server or
remote-access layer of any kind.

## Reachability

The phone reaches the dashboard over Tailscale (private VPN mesh) — no
port-forwarding, no public exposure, no TLS/auth work needed beyond what
Tailscale already provides.

## Architecture

```
 character process (Tkinter, existing)      character process (Tkinter, existing)
   tick() [client.py:7331] ──writes──┐         tick() ──writes──┐
                                     v                           v
                          webstate/<character>.json   webstate/<character>.json
                                     ^                           ^
                                     │ reads/writes               │ reads/writes
                                     └─────────────┬───────────────┘
                                                    │
                                              hub process (new, stdlib-only)
                                                    │
                                          ThreadingHTTPServer
                                          GET /api/state   POST /api/command
                                                    │
                                            dashboard page (HTML/JS, polling)
                                                    │
                                              phone browser (via Tailscale)
```

Three kinds of processes, all on the same machine:

1. **Character processes** (existing, modified). Each one already ticks
   once a second (`tick()`, client.py:7331). Add to that tick: write a
   per-character state snapshot to disk, and check the same file for a
   command queued from the phone.
2. **Hub process** (new). A small standalone Python script — no Tkinter,
   no MUD connection — that serves the dashboard and a tiny JSON API by
   reading/writing the `webstate/*.json` files and the existing
   `reminders.json`. Runs continuously; does not need to be a character.
3. **Dashboard page** (new). Static HTML/CSS/JS served by the hub.
   Polls `GET /api/state` every ~2s and renders it; submits commands via
   `POST /api/command`. No build step, no framework — plain JS is
   sufficient for a polling dashboard.

This mirrors the existing `reminders.json` pattern (a single shared JSON
file, read-modify-written via `paths.update_json`, polled once a second
by every open character) rather than introducing sockets between
processes. It keeps every Tkinter process single-threaded and means the
hub and the character processes can crash/restart independently without
affecting each other.

## Per-character state file

`webstate/<character>.json` under the install root (sibling to
`reminders.json`, same `BASE` directory defined in `katmud_lib/paths.py`),
written by that character's `tick()` using the same
temp-file-then-`os.replace` pattern as `paths.save_json`:

```json
{
  "updated": 1771113600.0,
  "room": "...",
  "idle_seconds": 42,
  "deadman_tripped": false,
  "output": ["...", "..."],
  "chats": [{"t": 1771113600.0, "line": "..."}],
  "tells": [{"t": 1771113600.0, "line": "...", "incoming": true}],
  "pending_command": null
}
```

- `output`, `chats`, `tells` are fixed-length ring buffers (e.g. last 200
  lines / 50 lines / 50 lines) — enough scrollback for a glance, not a
  full session log.
- `updated` is a timestamp the hub uses for staleness detection (see
  Error handling).
- `pending_command` is written by the hub (via `POST /api/command`) and
  cleared by the character process once it has executed it.

**Write ownership and the read-modify-write race**: both the hub
(setting `pending_command`) and the character process (writing its
snapshot, clearing `pending_command` after executing it) touch this file.
Both sides must use `paths.update_json`-style read-modify-write — the
hub's mutator only ever touches the `pending_command` key, the
character's mutator rewrites its own snapshot fields and clears
`pending_command` only if it just executed it — so neither side can
clobber the other's concurrent write. This is the same discipline
`update_json` already enforces for `reminders.json`; no new locking
primitive is needed.

## Hub process

Plain Python, `ThreadingHTTPServer` from the standard library — no new
dependency, consistent with this project's existing minimal-dependency
approach (the only optional dependency today is `keyring`).

Endpoints:

- `GET /` — the dashboard page (static HTML/JS, inlined or served from a
  file next to the hub script).
- `GET /api/state` — reads every `webstate/*.json` plus `reminders.json`,
  returns one aggregated JSON blob: list of characters with their
  output/chats/tells/status, plus the shared reminders list.
- `POST /api/command` — body `{"character": "...", "text": "..."}`;
  read-modify-writes that character's state file to set
  `pending_command`.

**Lifecycle**: the hub must already be running for the parking-lot
scenario to work, but the picker currently spawns characters and exits
(README.md: "spawns each client as a detached process and exits"). The
picker is extended to check whether the hub is already up (e.g. attempt
a connection to its known local port) and, if not, launch it as a
detached process the same way it launches character processes, before
spawning the requested character. The hub then stays up independent of
which characters are running or for how long.

## Dashboard UI

Single page, one stacked panel per character currently reporting state
(i.e. has a `webstate/<character>.json` updated recently — see staleness
below). Within each panel, in priority order:

1. **Tells** — own list, visually distinct/highlighted. Highest
   priority per the user's stated goal.
2. **Chats** — separate list, lower visual weight. Kept apart from
   tells per explicit requirement.
3. **Reminders** — the shared `reminders.json` list; same for every
   character panel since reminders aren't per-character.
4. **Mud output** — raw scrollback, collapsible/scrollable, lowest
   priority.
5. **Command box** — free-text input + send button. Submits via
   `POST /api/command`. No quick-action buttons in v1 — typing
   `Bot`, `Hunt`, `Go <area>` etc. is sufficient and keeps the UI in
   sync with whatever commands exist without separate UI maintenance.
   This also covers the secondary "go to another area and start
   another Hunt" goal — no dedicated UI for it.

Polling interval ~2s via `fetch`. No websockets/SSE — the use case
(periodic phone glances) doesn't need sub-second updates, and polling
keeps both the hub and the page trivial.

## Command + deadman interaction

A command submitted from the phone is executed by the character process
through the exact same code path as typing into the input box
(`on_enter`, client.py:7374) — not a separate "remote command" path. This
means it naturally updates `last_manual` and clears `deadman_tripped`
(tick(), client.py:7338-7344) exactly like keyboard input at the PC. This
is required for the primary scenario (wiz sends "bot check" while the
deadman has already tripped from sitting idle in the parking lot; user
must be able to respond from the phone and have it actually release the
gate) and was confirmed explicitly: phone activity counts as attention,
same as the PC keyboard.

## ntfy push notifications

Scope: **incoming tells only** — not chats, not reminders, not outgoing
tells. `mip_tell` (client.py:6156) already distinguishes incoming vs.
outgoing tells; the incoming branch additionally does a single
fire-and-forget HTTPS `POST` to a shared ntfy topic. This is done by the
character process directly, not routed through the hub, so push delivery
has no dependency on the hub being up.

- The topic name is a random, unguessable slug stored in `global.json`
  (new key, e.g. `settings.ntfy_topic`), since ntfy.sh public topics are
  world-readable/writable by anyone who knows the name. Self-hosting
  ntfy or adding auth is a possible future improvement, not needed now
  given Tailscale already gates the dashboard itself.
- POST uses a short timeout and swallows failures — a network blip must
  never block or delay the tick loop.

## Error handling

- **Hub down**: character processes are unaffected — they keep writing
  state files and pushing ntfy regardless. The dashboard simply fails to
  load until the hub is back up; the picker's auto-start check handles
  the common case of it not having been started yet.
- **Character process crashed/exited**: its state file stops updating.
  The hub treats a character as "offline" once `updated` is older than
  ~10s (one missed tick plus margin) and shows it as such rather than
  silently displaying frozen data.
- **Stale/conflicting `pending_command`**: governed by the
  read-modify-write discipline above — a command can't be double-
  executed or lost to a race between the hub's write and the character's
  claim-and-clear.

## Testing plan

No automated UI testing (this is a manual-use phone dashboard). Plan:

1. Unit tests for the state-file read/write and the `pending_command`
   claim logic (read-modify-write correctness), mirroring how reminders
   logic is tested today if such tests exist.
2. Manual live test with two character processes and the hub running
   simultaneously:
   - Confirm the dashboard shows both characters with correct
     output/chats/tells/reminders.
   - Send a real tell to a test character; confirm ntfy fires once, only
     for the incoming tell, not for the reply.
   - Let the deadman trip (or simulate it), send a command from the
     dashboard, confirm it executes in-game and the deadman releases.
   - Stop a character process and confirm the dashboard marks it
     offline within ~10s.

## Explicitly out of scope (this pass)

- HP bars, MIP data, map rendering on the dashboard.
- Push notifications for chats or reminders (tells only).
- Quick-action buttons for bot commands (free-text only).
- Authentication beyond Tailscale's network-level gating.
- Websocket/SSE real-time push to the browser (polling is sufficient).
