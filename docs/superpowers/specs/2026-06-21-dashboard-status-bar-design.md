# Phone/web dashboard — persistent status bar

Date: 2026-06-21

Follow-up to `docs/superpowers/specs/2026-06-21-web-dashboard-design.md` (the
original dashboard: hub process, per-character webstate files, tells/chats/
output panels, ntfy push, command box). This adds a persistent two-line
status bar to each character panel, plus caps tells/chats display to the
last 3 entries to make room for it.

## Purpose

At-a-glance visibility into room, idle time, deadman state, currency, and
combat/vitals state per character, without scrolling. Explicitly plain text
(no graphical bars) — visual HP bars remain out of scope per the original
spec's priority ordering.

## Data added to webstate

Three new fields on the existing per-character `webstate/<id>.json`
(written every tick by `_write_webstate`, same file/process as today — no
new write path):

- **`deadman_seconds_left`**: computed the same way `tick()` already
  computes it for the desktop status bar — `dm_limit - manual_idle` where
  `dm_limit = self.deadman_minutes * 60`. `null` if the deadman is disabled
  (`self.deadman_minutes` falsy) for that character.
- **`vitals`**: a direct snapshot of `self.vitals` — whatever keys are
  currently populated (e.g. `hp`/`hpmax`, `seid`/`seidmax`, `vig`/`vigmax`,
  `rad`/`radmax`, `enemy`, `enemycond`). This dict is already guild-generic
  in the existing client code (Viking, Gentech, and Changeling all write
  into it via the same `x`/`xmax` convention), so no per-guild branching is
  needed to populate it.
- **`daler`**: `self.viking_state.get("DALER")`. `self.viking_state` is
  unconditionally initialized to `{}` for every character regardless of
  guild (client.py:199), so this read is always safe — `null`/absent for
  non-Viking characters, a number for Viking ones.

`room` and `idle_seconds` are already present in webstate today and are
reused as-is for line 1.

## Dashboard rendering

Two lines inserted at the top of each character panel (above Tells), in
normal page flow — not capped/truncated like tells/chats, not fixed/sticky
to the viewport. They scroll away with the rest of that panel if you
scroll past it, same as everything else in the panel.

**Line 1**: `Room: <room>   Idle: <fmtDur(idle_seconds)>   <deadman>`,
where `<deadman>` is:
- `DM off` (grey, `#777788`) if `deadman_seconds_left` is `null`,
- `DM TRIPPED` (red, `#ff5555`) if `deadman_tripped` is true,
- `DM <fmtDur(deadman_seconds_left)>` (green, `#66cc66`) otherwise.

This mirrors the color logic the desktop status bar already uses
(client.py `tick()`, the `self.tb["deadman"]` block).

**Line 2**: built by a generic renderer over the `vitals` dict and the
top-level `daler` field, in this order:

1. If `daler` is present (not `null`/`undefined`): render `Daler <n>` with
   thousands separators.
2. For every key in `vitals` that has a matching `<key>max` sibling, and
   is not `enemy`/`enemycond`: render `Label cur/max` (label = the key
   capitalized, e.g. `hp` -> `Hp`).
3. If `vitals.enemy` is non-empty: render `Enemy: <name> <enemycond>%`.
4. Any other key present in `vitals` that wasn't consumed by steps 2-3 and
   doesn't end in `max`: render `key: value` as a plain fallback, so an
   unrecognized guild's stat shape still shows *something* rather than
   silently vanishing.

This keeps the renderer guild-agnostic: it inspects the shape of `vitals`
at render time rather than branching on which guild the character belongs
to, so a guild not yet using the `x`/`xmax` convention degrades to the raw
fallback instead of breaking.

## Tells/chats cap (companion change, already shipped this session)

Tells and chats are capped to the last 3 entries each in the dashboard's
rendering (`Array.slice(-3)` before display) — the full ring buffers (up
to 50/200 entries) are still written to webstate and still available via
`/api/state`; only the dashboard's display is capped, freeing vertical
space for the output panel and the new status bar.

## Testing

No automated UI test (same rationale as the original dashboard spec — this
is a manual-use phone tool). Manual verification:

1. Run a live Viking character; confirm the dashboard shows Room/Idle/DM
   on line 1 and Daler + Hp/Seid/Vig/Rad (+ Enemy when in combat) on line
   2, matching what the desktop status bar and Viking status window show
   for the same character at the same moment.
2. Trip the deadman (or simulate it) and confirm line 1 switches to `DM
   TRIPPED` in red, then confirm sending a command from the dashboard
   clears it.
3. Run a non-Viking character; confirm line 2 omits `Daler` entirely and
   still renders whatever `vitals` that guild populates (or nothing, if
   empty) without erroring.

## Explicitly out of scope (this pass)

- Graphical HP/vitals bars (plain text only).
- Per-guild custom labels beyond the generic `x`/`xmax` + `enemy` + raw
  fallback rules above.
- Sticky/fixed positioning of the status bar while scrolling.
