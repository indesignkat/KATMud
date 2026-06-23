# Mapless Bot/Hunt + Chaossea (Sea of Chaos hunt)

2026-06-20

## Problem

`Bot`/`Hunt` (`client.py` `_bot_*`) require the sqlite map backend and a
charted `area_id` for the current room: `_bot_start` refuses to run without
one, and `_bot_pick_move` picks a random exit from the map's stored exits
for the current vnum.

Some zones can't be charted at all - the user reports areas where the map
"loads randomly and all rooms share the same roomid." Every physical room
reports the identical vnum, so the sqlite map's per-vnum exit storage
collapses every distinct room in the maze into one DB row, overwritten on
each visit. Bot/Hunt would not error out (the reused vnum likely already
has *some* charted `area_id` from an earlier visit), but `_bot_pick_move`
would return nonsense - whatever exits happened to be recorded last for
that vnum, unrelated to the room actually being stood in.

One specific such zone, "Sea of Chaos," is a randomly-generated maze of up
to 7-8 floors that the user visits roughly once per server boot to find a
key item (`a chaotic charm`) carried by one of the zone's mutants.

## Goal

Two related pieces:

1. **A `mapless` mode for `Bot`/`Hunt`**, for blind wandering in an
   uncartographable zone: roam by direction only, fight whatever comes (or
   actively attack, for Hunt), no map required.
2. **`Chaossea`**, a dedicated command built on top of the mapless engine
   that automates the actual Sea of Chaos errand: explore the maze
   systematically (not blindly), examine mutants for two specific carried
   items, kill/loot the ones that matter, and retreat once the goal is
   met.

## Part 1: `Bot mapless` / `Hunt mapless`

### Activation

`_bot_command` (the shared handler for `cmd_bot`/`cmd_hunt`) gains a
`"mapless"` token alongside the existing `"off"`/`"debug"`:
`Bot mapless` / `Hunt mapless` → `self._bot_start(mode, mapless=True)`.
Everything else about invocation is unchanged.

### What changes vs. charted Bot/Hunt

`_bot_start`, when `mapless=True`:
- Skips the `map_backend == "sqlite"` / charted-`area_id` requirement.
- Sets `self._bot_mapless = True`; leaves `_bot_area_id`/`_bot_area_name`
  unset (not meaningful here). Status message says "wandering blind"
  instead of naming a charted area.

`_bot_tick`, when mapless:
- Skips the area-boundary check entirely (no area concept).
- On deadman trip or (Hunt-only) the no-combat-room limit
  (`wander_clear_limit`, new setting, default 30 / 0=off, mirrors
  `hunt_clear_limit`), calls a new `_bot_stop_in_place(reason)` instead of
  `_bot_home_step`: finish any current fight (same gating as today), then
  just stop where it is, with a message explaining it stopped in place
  rather than walking home (no coherent map to path through). Plain
  mapless `Bot` stays an unbounded passive roam, same as charted Bot
  today.

`_bot_pick_move`, when mapless, delegates to a new
`_bot_pick_move_mapless()`:
- Reads `self.exits` directly (kept live on every room by
  `sql_follow_ddd` regardless of charting state) rather than querying the
  sqlite map.
- Excludes the exact opposite of `self._bot_last_dir` (new field) when
  another option exists; allows the reversal if it's the only exit (dead
  end corridor).
- Plain uniform-random choice among what's left. (Hunt's minimap-weighted
  picking does not apply here - that minimap's biasing is itself relative
  to the charted map and meaningless in a random maze that has none.)
- If `self.exits` is empty, that's a real dead end: stop with "no exit
  from here."

### What does NOT change

Combat gating (`in_combat` checks), the room-ready race (prompt vs. room
packet), `_bot_assess` (non-party player skip, aggro-wait, Hunt's active
attack with engage-grace/whiff-cap), `_arm_post_kill_resume`/
`_post_kill_holding` (so guild post-kill holds like `vrest`/`revalrie`
still work), and the corpse-cascade kill trigger. None of this code is
map-aware; mapless mode reuses it unmodified.

## Part 2: `Chaossea`

A new dedicated command (`cmd_chaossea` / `self._chaossea_*` state),
built on the mapless engine's movement/combat plumbing but with its own
per-room loop and goal tracking. Not a flag on `Bot`/`Hunt` - the
examine/target/loot/retreat logic is specific enough to this zone to earn
its own command.

### Temp-map: a session-local graph without roomid verification

Sea of Chaos regenerates its layout each visit, so real vnums (even if
they didn't all collapse to one) wouldn't be reusable across visits
anyway - but within ONE visit the layout is assumed stable (otherwise no
systematic exploration of any kind, by a human or a bot, would be
possible). The temp-map exploits that: a graph of fake rooms
(`f00001`, `f00002`, ...), each a dict of `direction -> target fake room
(or unknown)`.

- Start: current room = `f00001`.
- Move through a direction **not yet recorded** from the current fake
  room: allocate a new fake room id, record the edge forward, and ALSO
  record the assumed reciprocal edge (opposite direction, new room back to
  the old one) - this is an assumption, not a verified fact (no roomid
  confirms it), but it's what lets backtracking collapse back to a known
  room instead of the map growing without bound. Sea of Chaos's apparent
  stability-within-a-visit is what makes this a reasonable bet.
- Move through a direction **already recorded**: reuse the known target,
  don't allocate a new room.
- This gives `Chaossea`'s movement picker a novelty preference: from the
  current fake room, prefer a direction not yet taken from here (drives
  systematic coverage of the floors) over a direction that's already
  known; only fall back to a known edge (excluding immediate reversal,
  per Part 1's rule) once every direction from the current room has been
  explored.

### Per-room loop

After the room display settles (same room-ready detection as Bot/Hunt):

1. Read the room's mob presence the existing way (italic-marker
   detection). Per the user, every mob in this zone IDs to the single
   keyword `mutant` regardless of its short description, so targeting
   doesn't need per-mob name parsing: `examine mutant`, `kill mutant`. For
   more than one mutant in the room, try ordinal disambiguation
   (`examine mutant 2`, `examine mutant 3`, ...) by count - **provisional,
   needs a live multi-mutant room to confirm this MUD's actual ordinal
   syntax.**
2. For each mutant present (by ordinal), `examine mutant [N]` and scan the
   response for the substrings `"a chaotic charm"` / `"a cube of raw
   chaos"`.
3. Decide whether to engage each examined mutant:
   - Carries an item still needed (charm if `!has_charm`, cube if
     `!has_cube`) → attack it.
   - It aggroes on its own (combat starts without our initiating it, same
     detection Bot already uses) → fight back, same handling as Bot's
     aggro mode.
   - It blocks movement (a direction that doesn't change rooms despite
     being a real exit) → attack it to clear the path. **Provisional -
     no captured wire text for a "blocked by a mob" message yet; the
     blocked-detection itself is a placeholder pending a live example.**
   - Otherwise → ignore, leave it alone, move on to the next mutant/room.
4. After a kill: `get all` (the cube isn't bind-on-pickup, so this is
   sufficient for it), then `get charm` specifically if that mutant
   carried one (bind-on-pickup skips `get all`). Update `has_charm`/
   `has_cube`.
5. **Goal check**, before continuing to move:
   - `has_charm && has_cube` → send `retreat from the sea`, stop the
     command (success).
   - `has_charm && !has_cube` → keep exploring, but only cube-carrying
     mutants are now targets (charm-carriers are ignored - already have
     one).
   - `!has_charm && has_cube` → keep exploring, only charm-carriers are
     targets.
   - Neither yet → both are targets.
6. Move: a separate picker (`_chaossea_pick_move`, not the same function
   as Part 1's `_bot_pick_move_mapless` - this one reads the persistent
   temp-map graph instead of the memoryless live `self.exits`) prefers an
   unrecorded direction from the current fake room; falls back to a
   recorded one, excluding immediate reversal, once none are left. Same
   *conceptual* novelty-preference/anti-backtrack rule as Part 1, applied
   to a graph instead of a single room's exits.

### Stop conditions

- Goal met (`has_charm && has_cube`): retreat + stop (success).
- Manual `Chaossea off`, disconnect: stop immediately (mid-room is fine,
  no in-progress multi-step state to unwind beyond the current
  examine/attack, which is safe to abandon).
- Deadman trip: finish any current fight, then stop in place (same
  reasoning as Part 1 - no verified path to "home" even though the
  temp-map has a graph, since reciprocal edges are an assumption, not a
  certainty. Not pathing automatically on deadman keeps the risk
  surface the same as plain mapless mode).
- No explicit "give up" room-count limit for `Chaossea` (unlike `Hunt
  mapless`'s `wander_clear_limit`) - the user's whole point is exhaustive
  floor coverage to find two specific items, so stopping early on a quiet
  stretch would defeat the purpose.

### Explicitly out of scope (per user)

- A "navigate back to the entrance fake room" fallback if `has_charm` but
  a cube can't be found - the user described this as the in-game
  mechanic's OWN second escape route (physically walking back to the
  entry room), not a behavior they asked the bot to automate. The
  temp-map's graph *could* support pathing back to `f00001` later if
  wanted, but it is not built now.
- Generalizing the item-hunt logic beyond Sea of Chaos - this is a
  zone-specific command, not a reusable "hunt for a named item" mode.

## Open items needing a live capture (provisional handling, flagged above)

- Exact `examine mutant [N]` output format, and the real ordinal syntax
  for disambiguating multiple same-named mutants in one room.
- Exact text for a movement attempt blocked by a mob.

Both will be implemented with a reasonable provisional guess and verified/
corrected against a live capture, consistent with how every other guild
feature in this codebase was built (capture-first, but ship a first pass
rather than block on it).
