# Stub-link inference (offline stub completer)

## Problem

3s areas accumulate exit stubs (`exits.to_vnum IS NULL`) during charting.
`Explore` fills these in by physically walking to each stub and probing it
live. But once an area's rooms are *already* fully charted, many remaining
stubs don't lead anywhere new — they lead to a room that's already in the
database, just not linked up yet. Walking each one to confirm that is slow
and requires an active MUD connection. This feature instead *infers* those
links from the existing map data: if a stub's direction implies a
coordinate, and a known room already sits at that coordinate, the stub
almost certainly leads there.

This is a pure data-analysis feature — it never needs the MUD. It can run
entirely offline against a mud's `.db` file.

## Algorithm — `MapDB.infer_stub_links(area_id)` (new method in `mapsql.py`)

1. **Pick an anchor room** for the area (placed at `(0,0,0)`):
   - The in-area room with an inbound exit from a room in a *different*
     area (the literal entrance other areas link into).
   - If several qualify, the lowest vnum wins. If none exist (e.g. an
     overland-rooted area with no recorded inbound door), fall back to the
     lowest vnum in the area.
2. **Lay out the area** via BFS from the anchor, following only
   non-timed (`wait_seconds=0`) exits whose direction has a geometric
   offset:
   - The 8 compass dirs (`mapsql.GRID_DIRS`) move x/y.
   - `u`/`d` move z by ±1.
   - Special/non-compass directions (e.g. `"climb over logs"`, `"shop"`)
     have no offset and are not used for placement — same rule the live
     map-pane renderer (`MapDB.neighborhood`) already follows.
   - If two rooms compute to the same `(x,y,z)`, that's a genuine
     non-planar collision: both are flagged and excluded as match targets
     (their coordinate can't be trusted).
3. **Match stubs to known rooms.** For every non-timed stub whose
   direction `d` has a geometric offset, compute the implied coordinate
   (stub's room coordinate + offset) and look for a known, non-colliding
   room `B` sitting there:
   - **No room there** → no candidate. (The common case — most stubs
     genuinely lead nowhere charted yet.)
   - **Exactly one room `B` there:**
     - `B` already has its own open stub in `REVERSE_DIRS[d]` → **high
       confidence**: link both sides (`A-d->B` and `B-rd->A`).
     - `B` has no exit row in `rd` at all → **medium confidence**: report
       only. Never fabricate an exit the MUD never showed — it could
       genuinely be one-way. (Matches the existing rule that charting
       never invents or deletes exits.)
     - `B`'s `rd` exit already points to a different room → **contradiction**:
       report only (same spirit as the existing `reverse_contradictions`
       detector).
   - **Coordinate is a collision cell** → report only, listing the
     candidate rooms competing for that cell.
   - Stubs with no geometric direction (true specials) can't be analyzed
     at all — listed separately as "not analyzable," the same bucket
     `Mapcheck` already uses for non-cardinal stubs.
4. **Apply.** High-confidence pairs are linked immediately via
   `link_exit` (both directions). Everything else is returned for
   reporting only, never written.

Return shape (used by both callers below): a dict/namedtuple with
`anchor`, `linked` (list of `(from, dir, to)`), `proposed` (list of
`(from, dir, to, reason)` where reason is `"no-reverse"` /
`"contradiction"` / `"collision"`), and `unanalyzable` (list of
`(from, dir)`).

## Persistence of coordinates

`rooms.x/y/z` already exist in the schema but are always `NULL` today (the
live map pane recomputes layout on the fly each time via
`MapDB.neighborhood`). This feature is the first thing to actually use
those columns:

- Every run **fully recomputes and overwrites** `x/y/z` for every room in
  the area from the current exit graph — strict cache semantics, never
  patched incrementally. This sidesteps staleness: a stale value can only
  exist between runs, and the next run always corrects it.
- Rooms unreachable from the anchor via geometric edges (e.g. only
  reachable through a special exit, or a disconnected pocket) keep
  `x/y/z = NULL` — they simply can't be placed.
- Collision-cell rooms still get *a* coordinate written (whichever the BFS
  visits first), so other tooling has something to show, but they're
  flagged in the collision report as not authoritative.

## Interfaces

Both, sharing the same `mapsql.MapDB.infer_stub_links` logic — no
duplicated business logic between them.

- **`tools/stub_link.py <mud> <area>`** — standalone script, opens
  `paths.map_db_file(mud)` directly via `MapDB`. No client, no MUD
  connection. `<area>` accepts either an `area_id` or an area name
  (case-insensitive match against `areas.name`). Prints a report: links
  applied, proposed links with reason, collisions, unanalyzable stubs.
  No `--apply`/confirm flag needed — high-confidence links only ever fill
  a `NULL to_vnum`, never overwrite an existing one, so applying them is
  safe and idempotent (re-running after another charting session just
  picks up newly-stubbed exits).
- **`Mapstub`** bare command (+ `#mapstub`), following the existing
  `Mapcheck`/`Mapwipe` convention: scopes to the current room's area via
  `self._sql_cur_vnum` → `_bot_area_of`, calls the same
  `infer_stub_links`, prints the same report via `write_local`. Pure
  convenience wrapper for when you happen to be connected; no live-state
  logic involved.

## Testing

This repo has no test framework; logic lives in `mapsql.py` and is
covered by its existing `--selftest` `__main__` block. Add a constructed
test area covering:
- a high-confidence pair (mutual open stubs) → both get linked,
- a medium case (target room has no reverse exit at all) → reported, not
  linked,
- a contradiction (target's reverse exit points elsewhere) → reported,
- a collision (two rooms computing to the same cell) → both flagged,
  neither used as a match target,
- a non-compass stub → bucketed as unanalyzable,
- `rooms.x/y/z` populated correctly for the placed rooms,
- idempotency: running `infer_stub_links` twice in a row produces no new
  links the second time.

## Out of scope

- No changes to `Explore`, `Chart`, or any live-charting path — this is
  purely an analysis pass over already-charted data.
- No fabrication of exits the MUD never showed (no inventing a reverse
  exit on a room that never listed one).
- No handling of areas with zero rooms or a single room (nothing to
  infer); the tool should just report "nothing to do."
