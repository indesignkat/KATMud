# Mapping & Pathfinding Redesign Spec — 3Scapes (3s)

## Context / Intent

This client currently maps using a TinTin-derived `.map` file and `speedruns.tin`, originally built for 3Kingdoms (3k). We are replacing this with a SQL-backed (SQLite) mapping and pathfinding system designed specifically for 3Scapes (3s).

**This spec covers only the mapping/pathfinding subsystem, and only for 3Scapes (3s).** 3Kingdoms (3k) is a different MUD with its own existing map (`.map`/`.tin`-based), which is untouched by this spec and continues to work as it currently does. 3k has no equivalent to 3s's embedded room vnums, so the client has historically had to assign its own IDs while mapping — this scheme is specific to 3k and should not be merged with or replaced by the 3s schema below. Some areas exist in both muds (and may even share layouts), but they are mapped completely independently, with separate vnum spaces — do not attempt to reconcile or cross-reference room IDs between the two systems.

The rest of the client (connection handling, UI, scripting, credential storage, etc.) is out of scope for now — review existing files for context, but don't restructure anything outside this subsystem unless it's a direct integration point.

Files most likely affected: `mapdata.py` (existing map logic — will largely be replaced), and integration points in `client.py` (movement handling, MIP parsing, visual map pane) and `config.py` (settings inheritance, landmark storage).

**Before implementing**, review:
- Existing `mapdata.py` and how it's used by `client.py`
- The mud/guild/player/global config inheritance system in `config.py`
- How MIPs are currently parsed in `client.py`

Then ask any clarifying questions about integration before starting. Implement in stages — see "Suggested implementation order" at the end. Don't implement the whole spec in one pass.

---

## Why 3s is different from 3k

3Scapes embeds real room ID numbers (vnums) directly in room short descriptions, and provides a `rating` command that reports the current area name and author. This means the map can be built automatically and accurately through normal play, rather than requiring hand-maintained `.map` files.

---

## Data sources from the MUD

### Room short descriptions
Format: `<description text> (<exit list>) [<vnum>]`

Example: `On a path to a farm (e,w) [113129]`

- The bracketed vnum is present on nearly all rooms.
- **Exception**: player house rooms do not have a bracketed vnum. These are identified via `rating` returning `AREA NAME: <name> [House]`. House rooms and their connections are handled by a separate player-specific submap, outside the main mud map (see "Out of scope" below).
- The exit list in parens is freeform text, comma-separated. Common cases:
  - Standard compass + a few extras: n, e, s, w, u, d, ne, nw, se, sw, plus area-specific tokens like "out", "enter", "leave", "portal"
  - Non-standard static tokens in some areas (e.g. "left"/"right" instead of e/w in ring-shaped areas) — these are stable per-area, just non-compass labels
  - One known area ("Hell") has exit labels that remap every few hours and is **explicitly out of scope** — see "Out of scope" below
- Description text itself may occasionally contain parentheses or brackets unrelated to exits/vnum — parsing should anchor on the exit-list and vnum being the trailing elements of the string, but **this needs validation against real samples** before the parser is considered reliable. Flag any room where parsing produces ambiguous/unexpected results rather than silently storing bad data.
- Some rooms may have no exit list shown at all (no visible exits, or all exits hidden).

### `rating` command
Format: `AREA NAME: <area name> [<author>]`

- Sent automatically by the client whenever the player enters a room not yet on the map, and swallowed (not shown to player).
- Used to tag the room with its area, and to detect area transitions (area name changes between consecutive rooms).
- Player houses report `AREA NAME: <house complex name> [House]` — used to identify and route these rooms to the house/submap system instead of the main map.

### MIPs (mud-information-protocol strings)
Sent every round; includes the player's current room vnum on movement, among other stats (HP, SP, uptime, etc.).

- **This should become the primary signal for "the player has moved to room X"**, since it's more robust than parsing short-description text for routine movement.
- Short-description parsing remains necessary for *charting new rooms* (getting description, exit list, vnum for a room not yet in the database) and for the visual "surrounding rooms" pane.
- **Needs verification**: add debug logging to compare MIP-reported vnum against short-description-parsed vnum during normal play, to confirm MIPs are reliable enough to be the primary movement signal. Do this early, before building heavily on the assumption.

---

## Out of scope (explicitly excluded from auto-mapping)

- **"Hell" area**: exit labels remap every few hours, including across logins/reboots. No automated mapping or pathing through this area. If a player's current location is in Hell, `go <landmark>` pathfinding should simply not find paths through it (it won't be in the rooms/exits tables at all, or should be flagged/excluded — implementer's choice on whether to exclude during charting or filter during pathfinding).
- **Player houses**: identified via `rating` returning `[House]`. Handled by the existing/separate player-specific submap system, not the rooms/exits tables described here. The main map should treat the *connection* between a house and the main map as a normal exit (it has a vnum on the main-map side), but rooms inside the house are not charted into the main `rooms` table.

---

## Schema (SQLite)

### `areas`
| column | type | notes |
|---|---|---|
| area_id | INTEGER PK | |
| name | TEXT | from `rating` |
| author | TEXT | from `rating` |

### `rooms`
| column | type | notes |
|---|---|---|
| vnum | INTEGER PK | |
| short_desc | TEXT | raw description text (exits/vnum stripped) |
| area_id | INTEGER FK -> areas | |
| x, y, z | INTEGER, nullable | for visual map placement |

### `exits`
| column | type | notes |
|---|---|---|
| from_vnum | INTEGER FK -> rooms | |
| direction | TEXT | canonical direction: one of n/e/s/w/u/d/ne/nw/se/sw/p/l/r/o (matches speedrun letter set below), or a non-standard static token (e.g. "left") for areas that use those instead of compass directions |
| to_vnum | INTEGER FK -> rooms, nullable | null until the far side has been charted |
| command | TEXT, nullable | actual command sent to move this direction, if different from the standard token for `direction` (e.g. "climb over logs"). Null = use standard token. |
| setup_command | TEXT, nullable | command sent *before* `command`, if the exit requires a precondition (e.g. "unlock door", "enter code 1234"). Null = no setup needed. |
| return_command | TEXT, nullable | the command needed to traverse this exit *in reverse* — i.e., what the player must send when standing in `to_vnum` to get back to `from_vnum`. Player-specified for special exits (see "mapfix" below). |
| wait_seconds | INTEGER, default 0 | delay before the exit becomes usable / before movement completes (e.g. waiting for a train, button-delay). Pathfinding should treat any exit with wait_seconds > 0 as excluded (see pathfinding rules). |

Composite key consideration: `(from_vnum, direction)` should probably be unique — a room shouldn't have two exits in the same canonical direction. Implementer's call on whether to enforce this at the DB level or in application logic.

### `landmarks`
| column | type | notes |
|---|---|---|
| tag | TEXT | the name used in `go <tag>` |
| vnum | INTEGER FK -> rooms | |
| scope | TEXT | one of "mud", "guild", "player" |
| scope_owner | TEXT, nullable | null for "mud" scope; guild name for "guild" scope; player name for "player" scope |

Resolution order for `go <tag>`: player scope first, then guild, then mud — first match wins, matching the existing config inheritance pattern (later/more-specific layers shadow earlier/general ones).

---

## Speedrun alias format

Format example: `;12en(open door)7s3d`

- Numeric prefix = repeat count for the following single-letter direction (e.g. `12e` = move east 12 times). No prefix = once.
- Predefined single-letter tokens: `n, e, s, w, u, d` (compass), plus `p` (portal), `l` (leave), `r` (enter), `o` (out), `t` (ne), `v` (se), `z` (sw), `q` (nw). These are player-editable but default to this set.
- Anything else (non-standard commands, including `setup_command` and/or `command` when they differ from the standard token for an exit's `direction`) is wrapped in parentheses and sent verbatim, in sequence. An exit needing both setup and a non-standard move command would render as two consecutive parenthetical groups: `(unlock door)(climb over logs)`.
- Generation logic: for each exit traversed, check whether `setup_command` is set (if so, emit `(setup_command)`), then check whether `command` is set and differs from the standard token for `direction` (if so, emit `(command)`; otherwise emit the standard letter, with run-length compression for consecutive repeats of the same letter).

---

## Pathfinding (`go <landmark>`)

- Algorithm: Dijkstra (not plain BFS), since exits are not uniform cost.
- Cost model: base cost per exit step (e.g. 1), suggest no additional weighting beyond the exclusion rule below — i.e., among non-excluded exits, fewest-steps is "fastest."
- **Exclusion rule**: any exit with `wait_seconds > 0` is excluded entirely from pathfinding — never routed through, even if it would be the only/shortest path.
- If no path exists from current room to the landmark's room without using excluded exits, return `no path found to <destination>` (don't fall back to using timed exits).
- Current room is always known via MIP-reported vnum (pending verification above).
- Output: a speedrun-format string per the format above, executed as a single batched command sequence.
- During traversal, the client should not advance its "current room" pointer until MIP confirms the move — this applies normally during charting/exploration too (see MIP section above), and matters especially for `setup_command`/gated exits where the move doesn't happen immediately.

---

## In-game exit editing ("mapfix")

For exits that need special handling (non-standard `command`, `setup_command`, or both) — e.g., an area where you must "climb over logs" to move northwest, or "unlock door" before a normal move:

- Player-invoked command (naming TBD, e.g. `mapfix <direction> <command> [<setup_command>] <return_command>`), issued while standing in the room where the special exit originates.
- Player specifies the canonical `direction`, the actual `command` to send, optionally a `setup_command`, and the `return_command` needed to come back from the destination room. The return command is player-specified rather than inferred, since special exits often have non-obvious or asymmetric return paths.
- This is for cases where the player is manually navigating (not using a speedrun) and wants the map to remember "to go this direction from here, send this command instead" for future speedrun generation and for normal movement convenience (e.g., pressing numpad-7 for nw sends "climb over logs" instead of "nw").
- Map rendering: despite the non-standard command, these exits render in the visual map pane according to their canonical `direction` (so "climb over logs" still draws as a normal nw connection) — this preserves the existing area-transition rendering logic (interior rooms within an area don't get incorrectly linked to rooms outside it).

---

## Suggested implementation order

1. Schema creation (SQLite file, tables as above) + basic loader/connection module. No charting or pathfinding yet — just the data layer, so it can be reviewed/tested in isolation.
2. MIP-vs-short-description verification (debug logging) — confirm MIP room-vnum reporting is reliable enough to be the primary movement signal.
3. Short-description and `rating` parsing — tested against a real batch of samples (gather these from actual play before/during this stage), including area-transition detection.
4. Auto-charting: on entering an unmapped room, parse short-desc + rating, insert into `rooms`/`exits`/`areas`.
5. Landmarks (schema already covered in step 1; add `go <tag>` resolution logic + basic single-room "go" as a sanity check before full pathfinding).
6. Pathfinding (Dijkstra, exclusion rules) + speedrun alias generation.
7. `mapfix` command for special-exit editing.
8. Visual map pane integration (rendering from new schema, area-transition-aware).

Stop after each stage for review/testing before continuing — don't implement multiple stages in one pass unless explicitly told to.
