# Hunt minimap-biased movement

2026-06-18

## Problem

The `Hunt` engine (`client.py` `_bot_*`, mode `"hunt"`) roams a charted area
looking for mobs to fight. Today `_bot_pick_move()` picks the next exit with
`random.choice` over the room's charted exits (minus immediate backtrack) -
it has no idea which neighboring room actually has a mob in it until it
walks there and reads the room's italic/underline markers.

The 3s engine renders a small ASCII minimap inline with every room display:
a cross of cells one and two rooms away in each cardinal direction, e.g.

```
                                                                -O-
                                                                 |
                                                                -O-
                                                             | | | | |
Aegis Global - Level 4 (e,w,s,n) [18473]                   #-O-O-@-O-1
                                                             | | | | |
                                                                -O-
                                                                 |
                                                                -1-
                                                                 |
```

`O` = empty. A digit 1-5 = that many things in the room; 6 or more renders
as `*`. The digit/`*` is color-coded: magenta = mob(s), green = a player (or
a player + mob together - green wins when both are present). This is enough
signal to stop picking exits blindly.

## Goal

Hunt should prefer walking toward a cell showing a magenta count over one
showing `O`/no-data, and should deprioritize a cell showing green (a known
player there - Hunt already pays a wasted step skipping those once it
arrives, so there's no reason to walk toward one on purpose). Bot (passive
aggro-soak roaming) is unaffected - it keeps its current uniform random
walk.

Only the radius-1 cell in each direction is used (the cell immediately
adjacent to `@`, both on the room-name line and one row above/below it).
Radius-2 cells are not parsed: they're a tie-break refinement, not needed to
answer "which of the exits I'm about to choose between has a mob right
next to it."

## Format details that drive the parser

- The minimap is column-anchored, not name-relative: across every example
  in `logs/normal-20260618.log` the `@` character lands at the same screen
  column regardless of room-name length (the renderer pads the name to a
  fixed width before appending the map). The N1/S1 single-cell rows (which
  carry no name text) line up at that same column.
- The number of columns shown varies per room. Some rooms render the full
  5-column cross (W2 W1 @ E1 E2); others render only 4 (e.g. `O-@-1-O-#`,
  missing W2) when a radius-2 cell isn't known. The boundary marker (`#`)
  appears only where there's a charted dead end; an unbounded/uncharted
  edge just leaves blank padding instead.
- Because of that variable width, **west/east must be read relative to
  `@`'s own position** (2 characters left/right), never by fixed column
  offset. North/south rows are identified by **shape** (a lone `-X-` token
  on an otherwise-blank line), not by counting a fixed number of lines
  above/below the room line, since the connector (`| | | | |`) line's
  width also varies with the column count.
- A room missing a given exit entirely (no charted direction that way)
  simply won't be queried, since the picker only looks up minimap data for
  directions that are real charted exits of the current room.

## Design

### Parsing (`_bot_scan_markers`, `client.py:3792`)

Extend the existing per-line scan that already runs while Hunt is waiting
for a room to finish displaying (`_bot_waiting_room`, populated from the
same `(spans, clean)` the italic/underline marker scan uses):

1. **Single-cell row detection.** For every line, test it against a "lone
   token" shape: optional leading boundary char, then `-<cell>-`, with
   nothing else but whitespace around it. If it matches, remember
   `(column_of_cell, char, spans)` as the most recent candidate row.
2. **Cross-row detection.** For every line, look for `@`. If found, confirm
   it sits inside a dash grid (the characters immediately flanking it are
   `-`), then:
   - Read the west cell at `@`'s index - 2, east cell at index + 2,
     directly from this line.
   - If a single-cell row candidate was captured *before* this line and its
     column matches `@`'s column, that candidate is north (N1).
   - Arm a "south pending" flag so that the *next* single-cell row
     candidate captured *after* this line, at the matching column, is
     recorded as south (S1). Only the first such row after the cross row is
     taken (radius-1 only) - the flag is cleared once consumed.
3. **Classify each captured cell:** `O` -> empty. A digit `1`-`5` or `*` ->
   look up the ANSI tag covering that character's column (walk the line's
   `spans` list summing chunk lengths until the index falls inside one) and
   read its foreground color: 35/95 -> `"mob"`, 32/92 -> `"player"`,
   anything else (unexpected color, or no tag) -> `None` (unknown, treated
   as neutral - same as `O`). Any other character (whitespace, missing
   data) -> `None`.
4. Store the four results in `self._bot_minimap = {"n": ..., "s": ...,
   "e": ..., "w": ...}` (values: `"mob"`, `"player"`, or `None`).

Reset `self._bot_minimap = {}` in `_bot_enter_room`, in the same place the
existing `_bot_room_mob`/`_bot_room_player` flags are cleared, so it always
reflects only the room currently being displayed.

This parsing only runs in Hunt mode (`self._bot_mode == "hunt"`), guarded
the same way the existing call site already gates marker scanning on
`self._bot_on`.

### Move selection (`_bot_pick_move`, `client.py:3808`)

Replace the final `random.choice(forward or cands)` with a weighted choice
(`random.choices`) over the same candidate list, using a per-exit weight
derived from `self._bot_minimap.get(direction)`:

- `"mob"` -> weight 6
- `"player"` -> weight 0.25
- anything else (`None`, or a direction with no minimap entry - e.g. `u`/
  `d`/diagonals, which this minimap never reports) -> weight 1 (today's
  uniform behavior)

The backtrack-avoidance step (preferring `forward` exits over all `cands`)
stays exactly as it is now; the weighting only changes how a choice is made
*within* whichever list that step produces. With no minimap data captured
(parse failure, or a guild/area without this feature) every weight is 1 and
the result is identical to the current uniform `random.choice`.

Weights are plain constants in `_bot_pick_move`, not settings - there's
nothing here for an MUD operator to tune per-character, and the codebase's
existing settings are all timing/threshold knobs, not these.

## Out of scope

- `Bot` (aggro) mode, persisting minimap data to the sqlite map, and any
  cross-room/lookahead pathing beyond the immediate exit choice.
- A live debug command to print raw color tags - not needed; the color
  mapping (magenta/green) is already confirmed by the user.

## 2026-06-19 update: radius-2 + dead-end weighting, and Hunt's clear limit

Radius-2 was originally cut from scope above; it's now parsed too, because
the wire already renders it (this was data being silently discarded, not
new instrumentation). Motivation: a charted dead end with nothing in either
of its two cells is something Hunt can already see without walking there -
no reason to spend a move finding that out the hard way.

`_bot_minimap[direction]` is now an `_MMCell(near, far, deadend)` namedtuple
instead of a bare `"mob"`/`"player"` string - `near`/`far` are radius-1/
radius-2 reads (same `"mob"`/`"player"`/`None` values as before), `deadend`
is `True` only when a `#` boundary token was actually seen on that side
(confirmed charted dead end - not just "we didn't render that far").

West/east: walked outward from `@` in steps of 2 (offsets 2, 4, then 6 for
a boundary-only check - the wire never renders a real cell past radius-2).
North/south: the existing single-pending-row state machine (one slot for
the nearest pre-`@` row, a one-shot post-`@` capture) is extended to two
slots/two captures, using the same shape-matching, so N2/S2 piggyback on
exactly the same race-prone-but-accepted mechanism N1/S1 already used.
Vertical dead-end detection is deliberately NOT attempted - no captured
example confirms where/whether `#` renders for a vertical dead end, so N/S
`deadend` stays hardcoded `False` until one is captured live. This is
conservative by construction: it can only ever fail to apply the dead-end
weight bucket to a real vertical dead end, never wrongly apply it.

`_mm_move_weight` gained a third bucket: `_MM_DEADEND_EMPTY_WEIGHT = 0.15`
when a side is a confirmed dead end with nothing at any captured depth -
deprioritized like a known player cell, but never excluded (a hard 0
breaks `random.choices` if every candidate direction ends up in this
bucket, and the format-sniffing here is fragile enough that exclusion
would be unsafe). A radius-2-only mob/player sighting gets the *same*
weight as a radius-1 sighting (`_MM_KIND_WEIGHTS`, near checked before far)
- no separate "weaker, more distant" tier, by deliberate choice (kept
simple; revisit only if live testing shows it mattering).

**Hunt's "area cleared" stop** (separate from the minimap work, landed in
the same change): `hunt_clear_limit` (setting, default 30, 0=off) - after
that many consecutive rooms with no combat, Hunt walks back to its start
room and stops, reusing the existing deadman walk-home machinery
(`_bot_homing`/`_bot_home_step`, which re-pathfinds via `mapdb.find_path`
every step rather than recording a reverse trail). Hunt-only - `Bot`
(aggro) keeps roaming forever, since passive aggro-soaking has no
"cleared" concept. This intentionally mirrors a feature the `Run` engine
(a different, fixed-route bot) had and lost in this same session: Run's
version recorded and reverse-walked a trail, which broke badly when
resuming a long route (re-walking 80+ already-cleared rooms tripped the
no-combat counter before reaching new content) - Hunt doesn't have that
resume problem (it's live roaming each session, not replaying a fixed
path from index 0), and reusing the pathfinding-based walk-home avoids
the fragility class that bit Run in the first place.

## Testing

No automated test harness exists for the bot engines (they're driven by
live MUD output). Validate by running `Hunt` in a charted area and watching
`Hunt debug` output (`_bot_debug_report` now appends `minimap={...}`) to
confirm captured minimap values per room match what's visible in the
client, then confirm Hunt's chosen direction trends toward magenta cells
over a multi-room run. Pending live test, same as other recent bot changes
in this project.

Known risk to watch for live: room-ready fires on whichever of the prompt
or the BAD/DDD packet arrives first. South is the last cell parsed (it's
captured from the row *after* the `@` line), so if the packet wins the
race and lands before the room text finishes, south can be silently
missed for that room - check the debug line's `minimap=` against what's
actually on screen, especially on packet-completed rooms. West/east (read
directly off the `@` line) and north (captured before the `@` line) aren't
exposed to this race. Worst case if it does drop: that direction just
keeps today's uniform weight, not a wrong decision. S2 (2026-06-19 addition)
is even more exposed to this same race, being parsed later still - confirm
it isn't being *always* dropped (occasional drops to neutral are fine and
expected) before trusting it.

Additional 2026-06-19 validation points: confirm a horizontal `#` boundary
actually produces `deadend=True` and the `0.15` weight in a live room with
varying column counts (4 vs 5, and the 1-cell-wide case where `#` sits
right after `near` with no `far` rendered at all); confirm whether/where a
vertical dead-end marker ever renders (currently assumed absent); run
`Hunt` with `hunt_clear_limit` at its default (30) in an already-cleared
area and confirm it walks home via re-pathfinding (not stuck) and stops
with a `[hunt] area cleared...` message, not a `[hunt] deadman...` one.

## 2026-06-20 fix: asymmetric dash assumption dropped real mob signals

Found via `logs/normal-20260619.log`: Hunt walked past a room with a mob
one step east (room `[18208]`, exits `e,s,n` - no west) and went north
into an empty 2-room dead end instead. Root cause: `_mm_scan_line`'s `@`-row
detector required **both** `clean[idx-1]` and `clean[idx+1]` to be `"-"`
before parsing either side. A room missing one cardinal exit renders that
side as blank, not a dash (confirmed live: `"...@-1-O"` with a space, not
`-`, to `@`'s west) - so the whole row, including the *other* side and the
north pending-row capture, was silently skipped whenever a room lacked
exactly one of its west/east exits. `_bot_minimap["e"]` never got the
`"mob"` entry, `_mm_move_weight("e")` fell back to neutral `1.0`, and the
random pick had even odds between the real mob and two dead ends.

Fixed by checking west/east independently (`has_w`/`has_e`), parsing
whichever side(s) actually show a dash. Same root cause applied to
`_MM_SINGLE_RE` (the N/S single-cell-row matcher), which also required
dashes on both sides of the cell character; a captured N1 row in the same
log rendered as `"O-"` (dash east only, no west) and would have failed to
match at all. Both sides are now independently optional there too.

## 2026-06-20 fix #2: rooms with no e/w exit at all, and depth >2 cells

Still seeing dead-end walks, found two more bugs via `logs/normal-
20260620.log` (same root cause family - bad assumptions about what the
renderer always shows):

**`@`-row never recognized when a room has *neither* a west nor east
exit** (e.g. exits `(n,s)` only - `@` flanked by blank on both sides, not
a dash on either). The 2026-06-19 fix changed the gate from `has_w and
has_e` to `idx>=0 and (has_w or has_e)`, which still requires at least one
dash - so it didn't cover this case either. Confirmed live at room
`[11002]` (exits `n,s`): the south pending-row state (`_mm_south_col`)
never got re-armed for this room, so a real mob two rows south (`"1"` at
the matching column) fell into the `elif self._mm_south_col is None`
branch instead and got queued as a *north* candidate for some unrelated
future room. Fixed by dropping the dash requirement entirely - the gate
is now just `idx >= 0` (found `@` at all). West/east scanning is already
individually gated on its own dash inside the block, so a room with
neither just skips both and falls through to draining north/arming south,
which is exactly what's needed regardless of which exits exist.

**`_mm_scan_side` hard-capped at radius-2**, on the documented assumption
"the wire never renders past radius-2." Confirmed false live at room
`[11003]` (exits `w,n,s` - no east): with east unused, the renderer
extended *west* a third cell out (`"1-O-O-@"` - the mob `1` sits at offset
6, where the old code only ever checked offset 6 for a `"#"` boundary
marker and discarded anything else found there). Fixed by removing the
fixed 3-step cap and walking outward in a plain loop until hitting a
non-cell/non-`#` character or the end of the line - `near` stays depth 0,
everything at depth ≥1 folds into `far` (a mob/player found at any depth
beats a plain-empty reading found at another depth, so a mob 3 rooms out
isn't masked by an empty cell 2 rooms out).

Verified by replaying both rooms from the log through the real (patched)
`_mm_scan_line`/`_mm_scan_side`: room `[11003]`'s west cell now reads
`far="mob"`; room `[11002]`'s south cell now reads `far="mob"` (weight
`6.0`) instead of being silently dropped. Still pending a live `Hunt`
run to confirm in practice - this is the second round of "fixed the
exact reported symptom, then found a deeper instance of the same
underlying bad assumption" for this feature; a third live test should
specifically watch rooms with very few exits (1-2 cardinals) since
that's where every bug so far has clustered.
