# Mapless Bot/Hunt + Chaossea Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `mapless` mode to the existing `Bot`/`Hunt` area-roaming engine (`katmud_lib/client.py`) for zones that can't be charted (every room reports the same vnum), and a new dedicated `Chaossea` command that builds a session-local "temp-map" of fake rooms from directions taken and automates the Sea of Chaos item hunt (examine mutants, kill/loot the ones carrying a target item, retreat once the goal is met).

**Architecture:** Both pieces extend `katmud_lib/client.py`, the single-file client that already hosts `Bot`/`Hunt`/`Run`/`Explore`. Mapless mode is a flag (`self._bot_mapless`) on the existing `_bot_*` engine, swapping out only the three map-dependent decision points (movement, area-boundary check, deadman/no-combat homing) - everything else (combat gating, room-ready detection, room marker assessment, post-kill resume) is reused unmodified. `Chaossea` is new, separate state (`self._chaossea_*`) with its own command, its own per-room loop, and its own movement picker over a persistent fake-room graph - but it reuses the same room-ready detection plumbing Bot/Hunt already have.

**Tech Stack:** Python 3, Tkinter (existing GUI client), no new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-20-mapless-wander-and-chaossea-design.md` - read this first for the full rationale; this plan implements it task by task.

## Global Constraints

- This repo has **no persisted test suite** (no `pytest`, no `test_*.py` files anywhere). Every "test" step in this plan is a throwaway verification script run via Bash (`python -c "..."` or a temp `.py` file you delete after confirming it passes) - do **not** create a new `tests/` directory or introduce a test framework; that would be an unrequested restructure of an established (lack of) convention.
- Follow the existing code style exactly: docstrings/comments explain *why*, not *what*; no comments restating the code; settings are read via `self.setting(key, default)`; guild/character-cascade settings already used by Bot/Hunt (`bot_aggro_wait_ms`, `bot_postcombat_ms`, `bot_safety_ms`, `hunt_settle_ms`, `hunt_engage_grace_ms`, `hunt_max_whiffs`, `autoattack_command`, `bot_refresh_command`, `bot_party_whitelist`) are reused as-is by `Chaossea` where applicable - do not duplicate them under new names.
- After every task: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"` must print no error before moving on.
- Commit after every task (small, working increments) - never bundle two tasks into one commit.
- Both `Bot`/`Hunt` and `Chaossea` are GUI-event-loop-driven (`self.root.after(...)` reschedules a tick method) - there is no live MUD connection to test against in this environment. Verification is: (a) syntax/logic checked via scratch scripts for pure-logic pieces (direction picking, temp-map graph), and (b) manual review that the control flow mirrors the existing, already-proven `_bot_tick`/`_bot_pick_move`/`_bot_assess` pattern. The user will live-test against the real MUD after dinner (mapless Bot/Hunt in a no-vnum zone) and on a future Sea of Chaos visit (Chaossea) - flag remaining live-only unknowns in your final summary, don't claim they're verified.

---

## Part 1: Mapless `Bot`/`Hunt`

### Task 1: `mapless` flag, command parsing, and `_bot_start` requirement skip

**Files:**
- Modify: `katmud_lib/client.py` (init block ~line 278-330, `_bot_command` ~line 3078, `_bot_start` ~line 3498)

**Interfaces:**
- Produces: `self._bot_mapless` (bool, instance attr), `self._bot_last_dir` (str or `None`, instance attr), module-level `_DIR_OPPOSITE` (dict, e.g. `_DIR_OPPOSITE["n"] == "s"`), `_bot_start(self, mode="aggro", mapless=False)` (extra kwarg, default preserves old behavior).

- [ ] **Step 1: Add the direction-opposite table and two new instance fields**

In `katmud_lib/client.py`, find the module-level constants near the top (next to `DEFAULT_KILL_PATTERNS` at line 65). Add directly after it:

```python
_DIR_OPPOSITE = {
    "n": "s", "s": "n", "e": "w", "w": "e", "u": "d", "d": "u",
    "ne": "sw", "sw": "ne", "nw": "se", "se": "nw",
}
```

In `__init__`, find this block (around line 278-284):

```python
        # --- area roaming bot (Bot / Hunt commands) ---
        self._bot_on = False
        self._bot_mode = "aggro"        # "aggro" (wait for aggro) | "hunt"
        self._bot_area_id = None
        self._bot_area_name = ""
        self._bot_start_vnum = None     # room Bot was started in (return point)
        self._bot_prev_vnum = None      # room we came from (avoid backtrack)
```

Change it to:

```python
        # --- area roaming bot (Bot / Hunt commands) ---
        self._bot_on = False
        self._bot_mode = "aggro"        # "aggro" (wait for aggro) | "hunt"
        self._bot_mapless = False       # `Bot mapless`/`Hunt mapless`: no
                                        # sqlite map needed, roam by direction
                                        # only (see _bot_pick_move_mapless)
        self._bot_last_dir = None       # mapless-only: last direction moved
                                        # (avoid immediately reversing it)
        self._bot_area_id = None
        self._bot_area_name = ""
        self._bot_start_vnum = None     # room Bot was started in (return point)
        self._bot_prev_vnum = None      # room we came from (avoid backtrack)
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output (success).

- [ ] **Step 3: Parse the `mapless` token in `_bot_command`**

Find `_bot_command` (around line 3078):

```python
    def _bot_command(self, arg, mode):
        verb = "Bot" if mode == "aggro" else "Hunt"
        a = arg.strip().lower()
        if a in ("off", "stop", "halt"):
            if self._bot_on:
                self._bot_stop("stopped")
            else:
                self.write_local(f"{verb} is not running.", "#cc9933")
            return
        if a == "debug":
            self._bot_debug = not self._bot_debug
            self.write_local(
                f"[bot] debug {'on' if self._bot_debug else 'off'} - "
                "reports per-room: ready time + trigger + mob/player flags + "
                "decision.", "#6699cc")
            return
        if self._bot_on:
            running = "Bot" if self._bot_mode == "aggro" else "Hunt"
            self.write_local(
                f"{running} already roaming '{self._bot_area_name}'. "
                f"{running} off to stop.", "#cc9933")
            return
        self._bot_start(mode)
```

Replace the final two lines (`if self._bot_on:` block and `self._bot_start(mode)`) with:

```python
        if self._bot_on:
            running = "Bot" if self._bot_mode == "aggro" else "Hunt"
            self.write_local(
                f"{running} already roaming"
                + (f" '{self._bot_area_name}'" if not self._bot_mapless
                   else " (mapless)")
                + f". {running} off to stop.", "#cc9933")
            return
        self._bot_start(mode, mapless=(a == "mapless"))
```

- [ ] **Step 4: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 5: Skip the charted-area requirement in `_bot_start` when mapless**

Find `_bot_start` (around line 3498):

```python
    def _bot_start(self, mode="aggro"):
        verb = "bot" if mode == "aggro" else "hunt"
        if self.map_backend != "sqlite" or self.mapdb is None:
            self.write_local(f"[{verb}] needs the sqlite map backend.",
                             "#cc6666")
            return
        v = self._sql_cur_vnum
        if v is None:
            self.write_local(f"[{verb}] location unknown - move a room so "
                             f"the map locates you, then {verb.title()}.",
                             "#cc6666")
            return
        area_id = self._bot_area_of(v)
        if area_id is None:
            self.write_local(f"[{verb}] this room has no charted area - "
                             "can't scope the hunt. Map it first.",
                             "#cc6666")
            return
        self._bot_on = True
        self._bot_mode = mode
        self._bot_area_id = area_id
        self._bot_area_name = self._area_name(area_id)
        self._bot_start_vnum = v
        self._bot_prev_vnum = None
```

Replace the whole signature-through-`_bot_prev_vnum` block with:

```python
    def _bot_start(self, mode="aggro", mapless=False):
        verb = "bot" if mode == "aggro" else "hunt"
        v = None
        area_id = None
        if not mapless:
            if self.map_backend != "sqlite" or self.mapdb is None:
                self.write_local(f"[{verb}] needs the sqlite map backend "
                                 f"(or '{verb.title()} mapless' in an "
                                 "uncartographable zone).", "#cc6666")
                return
            v = self._sql_cur_vnum
            if v is None:
                self.write_local(f"[{verb}] location unknown - move a room "
                                 f"so the map locates you, then "
                                 f"{verb.title()}.", "#cc6666")
                return
            area_id = self._bot_area_of(v)
            if area_id is None:
                self.write_local(f"[{verb}] this room has no charted area - "
                                 "can't scope the hunt. Map it first.",
                                 "#cc6666")
                return
        self._bot_on = True
        self._bot_mode = mode
        self._bot_mapless = mapless
        self._bot_last_dir = None
        self._bot_area_id = area_id
        self._bot_area_name = self._area_name(area_id) if area_id else ""
        self._bot_start_vnum = v
        self._bot_prev_vnum = None
```

- [ ] **Step 6: Update the two status messages later in `_bot_start` to handle the mapless case**

Still in `_bot_start`, find (a few lines further down):

```python
        if mode == "hunt":
            aa = (self.setting("autoattack_command", "") or "").strip()
            how = (f"autoattack '{aa}'" if aa else "kill <keyword>")
            clear = (f", or {self.hunt_clear_limit} rooms with no combat,"
                     if self.hunt_clear_limit else "")
            self.write_local(
                f"[hunt] roaming area '{self._bot_area_name}' - {markers}; "
                f"attacks any mob it finds via {how}, skips rooms with a "
                "non-party player. Hunt off to stop. (On deadman"
                f"{clear} it walks back to start.)",
                "#66cc66")
        else:
            self.write_local(
                f"[bot] roaming area '{self._bot_area_name}' - {markers}; "
                "fights aggro as it comes, skips rooms with a non-party "
                "player. Bot off to stop. (On deadman it walks back to "
                "start.)",
                "#66cc66")
```

Replace with:

```python
        where = (f"area '{self._bot_area_name}'" if not mapless
                 else "blind (mapless - no map, no area)")
        home = ("stops in place" if mapless else "walks back to start")
        if mode == "hunt":
            aa = (self.setting("autoattack_command", "") or "").strip()
            how = (f"autoattack '{aa}'" if aa else "kill <keyword>")
            limit = self.wander_clear_limit if mapless else self.hunt_clear_limit
            clear = (f", or {limit} rooms with no combat," if limit else "")
            self.write_local(
                f"[hunt] roaming {where} - {markers}; "
                f"attacks any mob it finds via {how}, skips rooms with a "
                f"non-party player. Hunt off to stop. (On deadman{clear} it "
                f"{home}.)",
                "#66cc66")
        else:
            self.write_local(
                f"[bot] roaming {where} - {markers}; "
                "fights aggro as it comes, skips rooms with a non-party "
                f"player. Bot off to stop. (On deadman it {home}.)",
                "#66cc66")
```

This references `self.wander_clear_limit`, added in Task 3 - that's fine, Python resolves it at call time, not at parse time.

- [ ] **Step 7: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Add mapless flag scaffolding to Bot/Hunt (Bot mapless / Hunt mapless)"
```

---

### Task 2: Mapless movement picker

**Files:**
- Modify: `katmud_lib/client.py` (`_bot_pick_move` ~line 4057, `_bot_tick`'s move-send site ~line 3658)

**Interfaces:**
- Consumes: `_DIR_OPPOSITE`, `self._bot_mapless`, `self._bot_last_dir`, `self.exits` (list of lowercase direction strings, kept live by `sql_follow_ddd` regardless of charting).
- Produces: `_bot_pick_move_mapless(self)` returns a direction string (e.g. `"n"`) or `None` (dead end - no exits at all); `_bot_send_move_mapless(self, direction)`.

- [ ] **Step 1: Write the picker**

Find `_bot_pick_move` (around line 4057):

```python
    def _bot_pick_move(self):
        """A random charted exit whose destination is in the hunt area,
        preferring not to immediately backtrack. Skips timed (wait>0) and
        unmapped (to_vnum NULL) exits."""
        v = self._sql_cur_vnum
```

Add a new method directly above it:

```python
    def _bot_pick_move_mapless(self):
        """Plain mapless wander: pick a random direction from the CURRENT
        room's live exit list (self.exits, kept fresh every room regardless
        of charting), excluding the exact opposite of the last direction
        moved when another option exists (the closest mapless equivalent of
        the charted picker's anti-backtrack rule - there's no vnum here to
        compare against). Returns None if there are no exits at all (a real
        dead end, not a charting gap)."""
        exits = list(self.exits or [])
        if not exits:
            return None
        reverse = _DIR_OPPOSITE.get(self._bot_last_dir)
        forward = [d for d in exits if d != reverse]
        pool = forward or exits
        return random.choice(pool)

    def _bot_send_move_mapless(self, direction):
        self._bot_last_dir = direction
        self.send_line(direction)
```

- [ ] **Step 2: Verify the anti-backtrack logic against the real method**

`katmud_lib/client.py` imports cleanly without creating a Tk root (confirmed: `python -c "import katmud_lib.client"` succeeds), so call the actual unbound method against a minimal duck-typed stand-in instead of reimplementing its logic - this exercises the real code, not a copy of it. Run this (not committed - throwaway verification):

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from types import SimpleNamespace
import katmud_lib.client as c

c.random.seed(1)

# came from the south (moved 'n' to get here), exits n/s/e available ->
# should never pick 's' (the reverse) while n/e exist
fake = SimpleNamespace(exits=['n', 's', 'e'], _bot_last_dir='n')
seen = {c.MudClient._bot_pick_move_mapless(fake) for _ in range(200)}
assert seen == {'n', 'e'}, seen

# only exit is the reverse -> must still return it (dead end corridor)
fake2 = SimpleNamespace(exits=['s'], _bot_last_dir='n')
assert c.MudClient._bot_pick_move_mapless(fake2) == 's'

# no last direction yet (first move) -> any exit is fair game
fake3 = SimpleNamespace(exits=['n', 's'], _bot_last_dir=None)
seen3 = {c.MudClient._bot_pick_move_mapless(fake3) for _ in range(200)}
assert seen3 == {'n', 's'}, seen3

# no exits at all -> None
fake4 = SimpleNamespace(exits=[], _bot_last_dir='n')
assert c.MudClient._bot_pick_move_mapless(fake4) is None
print('OK')
"
```

Expected output: `OK`

- [ ] **Step 3: Dispatch to the mapless picker from `_bot_tick`'s move site**

Find, in `_bot_tick` (around line 3636-3660):

```python
        # --- pick and send the next roam step ---
        e = self._bot_pick_move()
        if e is None:
            self._bot_stop(f"no charted exit within '{self._bot_area_name}' "
                           "from here"); return
```

and a few lines later:

```python
        self._bot_prev_vnum = self._sql_cur_vnum
        self._bot_send_move(e)
        self._bot_enter_room(refresh=False)     # entry auto-look carries markers
```

Replace the first block with:

```python
        # --- pick and send the next roam step ---
        if self._bot_mapless:
            d = self._bot_pick_move_mapless()
            if d is None:
                self._bot_stop("no exit from here"); return
        else:
            e = self._bot_pick_move()
            if e is None:
                self._bot_stop(f"no charted exit within "
                               f"'{self._bot_area_name}' from here"); return
```

And replace the second block with:

```python
        self._bot_prev_vnum = self._sql_cur_vnum
        if self._bot_mapless:
            self._bot_send_move_mapless(d)
        else:
            self._bot_send_move(e)
        self._bot_enter_room(refresh=False)     # entry auto-look carries markers
```

(Leave the Hunt-only no-combat-counter block between these two, unchanged for now - it still references `self._bot_mode == "hunt"` and `self.hunt_clear_limit`, both of which Task 3 updates for the mapless case.)

- [ ] **Step 4: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Add mapless movement picker (direction-only, anti-backtrack) for Bot/Hunt"
```

---

### Task 3: Skip area-boundary check; stop-in-place instead of walk-home when mapless

**Files:**
- Modify: `katmud_lib/client.py` (`_bot_tick` area-boundary check + deadman block + no-combat-limit block; new `_bot_stop_in_place`; settings init block ~line 487)

**Interfaces:**
- Consumes: `self._bot_mapless`.
- Produces: `self.wander_clear_limit` (int, mirrors `self.hunt_clear_limit`), `_bot_stop_in_place(self, reason)`.

- [ ] **Step 1: Add the `wander_clear_limit` setting**

Find (around line 487):

```python
        self.hunt_clear_limit = int(self.setting("hunt_clear_limit", 30) or 0)
```

Add directly after it:

```python
        self.wander_clear_limit = int(
            self.setting("wander_clear_limit", 30) or 0)
```

- [ ] **Step 2: Add `_bot_stop_in_place`**

Find `_bot_home_step` (around line 4230):

```python
    def _bot_home_step(self):
        """Walk-home return (deadman trip, or Hunt's area-cleared limit):
```

Add a new method directly above it:

```python
    def _bot_stop_in_place(self, reason):
        """Mapless equivalent of walking home: there's no verified path back
        to anywhere (the temp-map's reciprocal edges, where one even exists,
        are an assumption, not a proof - see the design doc), so the safest
        thing on a deadman trip or the no-combat limit is to just stop right
        here rather than guess a route. Combat is never abandoned mid-fight -
        callers only invoke this once self.in_combat is already False."""
        self._bot_stop(f"{reason} (mapless - stopped in place, no map to "
                       "walk home through)")

```

- [ ] **Step 3: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 4: Branch the deadman block on `_bot_mapless`**

Find, in `_bot_tick` (around line 3589-3598):

```python
        if self.deadman_tripped:
            if self.in_combat:
                self._bot_reschedule(1.0); return   # don't leave mid-fight
            if not self._bot_homing:
                self._bot_homing = True
                self._bot_home_reason = ("deadman tripped - returning to "
                                         "start room, then stopping.")
                self.write_local(f"[bot] {self._bot_home_reason}", "#ffaa33")
                self._bot_home_step()
            return                                  # _bot_home_step drives now
```

Replace with:

```python
        if self.deadman_tripped:
            if self.in_combat:
                self._bot_reschedule(1.0); return   # don't leave mid-fight
            if self._bot_mapless:
                self._bot_stop_in_place("deadman tripped"); return
            if not self._bot_homing:
                self._bot_homing = True
                self._bot_home_reason = ("deadman tripped - returning to "
                                         "start room, then stopping.")
                self.write_local(f"[bot] {self._bot_home_reason}", "#ffaa33")
                self._bot_home_step()
            return                                  # _bot_home_step drives now
```

- [ ] **Step 5: Skip the area-boundary check when mapless**

Find (around line 3618-3621):

```python
        # --- area boundary ---
        if self._bot_area_of(self._sql_cur_vnum) != self._bot_area_id:
            self._bot_stop(f"left area '{self._bot_area_name}' - "
                           "re-Bot where you want to hunt"); return
```

Replace with:

```python
        # --- area boundary (mapless has no area concept - skip) ---
        if not self._bot_mapless and \
                self._bot_area_of(self._sql_cur_vnum) != self._bot_area_id:
            self._bot_stop(f"left area '{self._bot_area_name}' - "
                           "re-Bot where you want to hunt"); return
```

- [ ] **Step 6: Branch the Hunt-only no-combat-limit block on `_bot_mapless`**

Find (around line 3641-3657, the block right after the move-pick from Task 2 Step 3):

```python
        if self._bot_mode == "hunt":
            self._bot_no_combat_rooms += 1
            if self.hunt_clear_limit and \
                    self._bot_no_combat_rooms >= self.hunt_clear_limit:
                self._bot_home_reason = (
                    f"area cleared - no combat in {self._bot_no_combat_rooms} "
                    "rooms, returning to start room, then stopping.")
                self._bot_homing = True
                self.write_local(f"[hunt] {self._bot_home_reason}", "#ffaa33")
                self._bot_home_step()
                return
```

Replace with:

```python
        if self._bot_mode == "hunt":
            self._bot_no_combat_rooms += 1
            limit = (self.wander_clear_limit if self._bot_mapless
                     else self.hunt_clear_limit)
            if limit and self._bot_no_combat_rooms >= limit:
                reason = (f"area cleared - no combat in "
                         f"{self._bot_no_combat_rooms} rooms")
                if self._bot_mapless:
                    self._bot_stop_in_place(reason); return
                self._bot_home_reason = (
                    f"{reason}, returning to start room, then stopping.")
                self._bot_homing = True
                self.write_local(f"[hunt] {self._bot_home_reason}", "#ffaa33")
                self._bot_home_step()
                return
```

- [ ] **Step 7: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Stop in place (not walk-home) on deadman/no-combat-limit when mapless"
```

---

### Task 4: Help text and capitalized-command registration

**Files:**
- Modify: `katmud_lib/client.py` (help text ~line 8550, already-registered capitalized commands at ~line 7076 - `Bot`/`Hunt` are already there, no change needed there)

**Interfaces:** None new - documentation only.

- [ ] **Step 1: Update the help text**

Find (around line 8550-8559):

```python
  Bot | Bot off            (roam the current room's AREA, fighting aggro
      mobs as they engage, then moving on; needs the sqlite map and the
      two room-marker asets - see #markers. Empty rooms are skipped
      instantly; rooms with a non-party player are ceded.)
  Hunt | Hunt off          (like Bot but ACTIVELY attacks any mob it finds:
      uses guild setting autoattack_command if set (a '{t}' token is replaced
      with the mob keyword from the italic line), else 'kill <keyword>'. Skips
      non-party players. Party = pwho roster + setting bot_party_whitelist.
      Same deadman return-to-start. Biases movement toward the 3s minimap's
      mob cells when present. Toggle flat makes it skip up/down exits.)
```

Replace with:

```python
  Bot | Bot mapless | Bot off   (roam the current room's AREA, fighting
      aggro mobs as they engage, then moving on; needs the sqlite map and
      the two room-marker asets - see #markers. Empty rooms are skipped
      instantly; rooms with a non-party player are ceded.
      'Bot mapless' drops the map requirement entirely - roams by direction
      only, for zones that can't be charted, e.g. every room sharing one
      vnum. No area-left stop; deadman trip stops in place, not walk-home.)
  Hunt | Hunt mapless | Hunt off   (like Bot but ACTIVELY attacks any mob
      it finds: uses guild setting autoattack_command if set (a '{t}' token
      is replaced with the mob keyword from the italic line), else
      'kill <keyword>'. Skips non-party players. Party = pwho roster +
      setting bot_party_whitelist. Same deadman return-to-start (mapless:
      stops in place instead). Biases movement toward the 3s minimap's mob
      cells when present (charted only - mapless has no minimap to bias
      from). Toggle flat makes it skip up/down exits. wander_clear_limit
      (default 30, 0=off) replaces hunt_clear_limit's no-combat stop when
      mapless.)
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Document Bot mapless / Hunt mapless in client help text"
```

---

## Part 2: `Chaossea`

### Task 5: Temp-map graph (pure logic, no client state yet)

**Files:**
- Modify: `katmud_lib/client.py` (new methods, placed near `_bot_pick_move_mapless` from Task 2 - end of the `# ---- area roaming bot` section, before `# ------------------------------------------------ path bot (Run)` at ~line 4263)

**Interfaces:**
- Produces: `self._chaossea_map` (dict: `{fake_vnum: {direction: fake_vnum}}`), `self._chaossea_cur` (str, current fake vnum), `self._chaossea_next_id` (int counter), `_chaossea_alloc_room(self)` -> new fake vnum string (`"f00001"`, `"f00002"`, ...), `_chaossea_move(self, direction)` -> the fake vnum now current (allocates + records reciprocal edge if `direction` is new from `_chaossea_cur`, else reuses the known edge), `_chaossea_pick_move(self, exits)` -> a direction string from `exits` (novelty-preferring: unrecorded direction from `_chaossea_cur` first, else any recorded one excluding the immediate reverse, excluding nothing if that's the only option) or `None` if `exits` is empty.

- [ ] **Step 1: Write the temp-map methods**

Find the comment marking the end of the Bot/Hunt section (around line 4263):

```python
    # ------------------------------------------------ path bot (Run)
```

Add the following directly above it:

```python
    # ------------------------------------------------ Chaossea temp-map
    # A session-local graph for zones that regenerate their layout per visit
    # and collapse every room to one vnum (Sea of Chaos): fake room ids
    # (f00001, f00002, ...) connected by DIRECTIONS TAKEN, no roomid
    # verification at all. Moving through a direction not yet recorded from
    # the current fake room allocates a new one and records BOTH the
    # forward edge and the assumed reciprocal (opposite-direction) edge back
    # - an assumption, not a proof, but the only way to recognize
    # backtracking instead of the map growing without bound. See the design
    # doc (docs/superpowers/specs/2026-06-20-mapless-wander-and-chaossea-
    # design.md) for why this is a reasonable bet for this specific zone.
    def _chaossea_alloc_room(self):
        self._chaossea_next_id += 1
        return f"f{self._chaossea_next_id:05d}"

    def _chaossea_move(self, direction):
        """Record (or reuse) the edge for `direction` from the current fake
        room, advance _chaossea_cur to the result, and return it."""
        cur = self._chaossea_cur
        edges = self._chaossea_map.setdefault(cur, {})
        target = edges.get(direction)
        if target is None:
            target = self._chaossea_alloc_room()
            edges[direction] = target
            opp = _DIR_OPPOSITE.get(direction)
            if opp:
                self._chaossea_map.setdefault(target, {})[opp] = cur
        self._chaossea_cur = target
        return target

    def _chaossea_pick_move(self, exits):
        """Novelty-preferring pick over the live room exits: prefer a
        direction not yet recorded from the current fake room (drives
        systematic floor coverage); once every direction here is known,
        fall back to a known one, excluding the immediate reverse of the
        last move unless it's the only option. Returns None if `exits` is
        empty (a real dead end)."""
        exits = list(exits or [])
        if not exits:
            return None
        known = self._chaossea_map.get(self._chaossea_cur, {})
        unexplored = [d for d in exits if d not in known]
        if unexplored:
            return random.choice(unexplored)
        reverse = _DIR_OPPOSITE.get(self._chaossea_last_dir)
        forward = [d for d in exits if d != reverse]
        return random.choice(forward or exits)

```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 3: Verify the graph + picker logic against the real methods**

Call the actual unbound methods against a minimal duck-typed stand-in (`types.SimpleNamespace`), not a reimplementation. Note `_chaossea_move` itself does NOT set `_chaossea_last_dir` - that's the tick's job (Task 8 sets it right after calling `_chaossea_move`) - so the test sets it explicitly between moves, matching how the real call site will use it.

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from types import SimpleNamespace
import katmud_lib.client as c

m = SimpleNamespace(_chaossea_map={}, _chaossea_cur='f00001',
                    _chaossea_next_id=1, _chaossea_last_dir=None)
assert m._chaossea_cur == 'f00001'

# go east into unexplored territory -> new room, reciprocal west edge exists
f2 = c.MudClient._chaossea_move(m, 'e')
assert f2 == 'f00002'
assert m._chaossea_map['f00001']['e'] == 'f00002'
assert m._chaossea_map['f00002']['w'] == 'f00001'
m._chaossea_last_dir = 'e'

# go back west -> reuses f00001, does NOT allocate a third room
back = c.MudClient._chaossea_move(m, 'w')
assert back == 'f00001'
assert m._chaossea_next_id == 2   # still only 2 rooms ever allocated
m._chaossea_last_dir = 'w'

# from f00001, 'e' is now known (goes to f2); picker prefers an unexplored
# direction ('s') over the known one when both are offered
c.random.seed(0)
choice = c.MudClient._chaossea_pick_move(m, ['e', 's'])
assert choice == 's', choice

# once every exit from this room is known, falls back to a known one,
# excluding the immediate reverse if another option exists
m2 = SimpleNamespace(
    _chaossea_map={'f00001': {'n': 'f00002', 's': 'f00003'}},
    _chaossea_cur='f00001', _chaossea_last_dir='n')   # last move was n
seen = {c.MudClient._chaossea_pick_move(m2, ['n', 's']) for _ in range(200)}
assert seen == {'n'}, seen   # never picks s (the reverse) while n exists

# dead end (only the reverse available) -> still returns it
m3 = SimpleNamespace(_chaossea_map={'f00001': {'n': 'f00002'}},
                     _chaossea_cur='f00001', _chaossea_last_dir='n')
assert c.MudClient._chaossea_pick_move(m3, ['n']) == 'n'

# no exits at all
assert c.MudClient._chaossea_pick_move(m3, []) is None

print('OK')
"
```

Expected output: `OK`

- [ ] **Step 4: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Add Chaossea temp-map graph (fake rooms, reciprocal edges, novelty-preferring picker)"
```

---

### Task 6: `Chaossea` command scaffolding (start/stop, state, registration)

**Files:**
- Modify: `katmud_lib/client.py` (init block, new `cmd_chaossea`/`_chaossea_start`/`_chaossea_stop`, capitalized-command list ~line 7076, command dispatch ~line 8206, help text ~line 8559)

**Interfaces:**
- Consumes: Task 5's `_chaossea_move`/`_chaossea_pick_move`/`_chaossea_alloc_room`.
- Produces: `self._chaossea_on` (bool), `self._chaossea_has_charm`/`self._chaossea_has_cube` (bool), `self._chaossea_job` (the `root.after` handle), `cmd_chaossea(self, arg)`, `_chaossea_start(self)`, `_chaossea_stop(self, reason)`.

- [ ] **Step 1: Add Chaossea state to `__init__`**

Find the end of the Bot/Hunt state block - the party-whitelist comment a few lines after `self._bot_minimap = {}` (around line 328-330):

```python
        # party whitelist: auto roster from `pwho` + manual bot_party_whitelist
        self._bot_party = set()         # lowercase names from the last pwho
```

Add a new block directly after the `_bot_party` line and whatever follows it in that paragraph (find where that paragraph ends - it's followed by a blank line and the next `# ---` section comment; insert there):

```python
        # --- Chaossea: Sea of Chaos item hunt (mapless temp-map + examine/
        # target/loot/retreat loop). See docs/superpowers/specs/2026-06-20-
        # mapless-wander-and-chaossea-design.md. ---
        self._chaossea_on = False
        self._chaossea_job = None
        self._chaossea_map = {}         # fake_vnum -> {direction: fake_vnum}
        self._chaossea_cur = "f00001"
        self._chaossea_next_id = 1
        self._chaossea_last_dir = None
        self._chaossea_has_charm = False
        self._chaossea_has_cube = False
        self._chaossea_room_mobs = 0    # mutant count seen this room
        self._chaossea_mob_idx = 0      # which ordinal (1-based) we're on
        self._chaossea_examine_buf = []
        self._chaossea_examine_pending = False
        self._chaossea_waiting_room = False
        self._chaossea_room_ready = False
        self._chaossea_room_mob = False
        self._chaossea_room_player = False
        self._chaossea_mob_lines = []
        self._chaossea_blocked_dir = None  # direction we just tried that
                                           # didn't change rooms (see Task 9)
```

- [ ] **Step 2: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 3: Add `cmd_chaossea`, `_chaossea_start`, `_chaossea_stop`**

Find the same insertion point used in Task 5 (directly above `# ------------------------------------------------ Chaossea temp-map`, i.e. right before that whole block you added in Task 5). Insert this new block there instead (above the temp-map comment, so the temp-map helper methods follow it):

```python
    def cmd_chaossea(self, arg):
        a = arg.strip().lower()
        if a in ("off", "stop", "halt"):
            if self._chaossea_on:
                self._chaossea_stop("stopped")
            else:
                self.write_local("Chaossea is not running.", "#cc9933")
            return
        if self._chaossea_on:
            self.write_local(
                "Chaossea already running. Chaossea off to stop.",
                "#cc9933")
            return
        self._chaossea_start()

    def _chaossea_start(self):
        self._chaossea_on = True
        self._chaossea_map = {}
        self._chaossea_cur = "f00001"
        self._chaossea_next_id = 1
        self._chaossea_last_dir = None
        self._chaossea_has_charm = False
        self._chaossea_has_cube = False
        self._chaossea_room_mobs = 0
        self._chaossea_mob_idx = 0
        self._chaossea_examine_buf = []
        self._chaossea_examine_pending = False
        self._chaossea_waiting_room = True
        self._chaossea_room_ready = False
        self._chaossea_room_mob = False
        self._chaossea_room_player = False
        self._chaossea_mob_lines = []
        self._chaossea_blocked_dir = None
        self.write_local(
            "[chaossea] hunting for a chaotic charm + cube of raw chaos - "
            "examines every mutant, fights the ones carrying a needed item "
            "(or that aggro/block you), retreats once both are found. "
            "Chaossea off to stop.", "#66cc66")
        self._chaossea_reschedule(0.6)

    def _chaossea_stop(self, why=""):
        self._chaossea_on = False
        if self._chaossea_job:
            self.root.after_cancel(self._chaossea_job)
            self._chaossea_job = None
        self.write_local(f"[chaossea] {why or 'off'}.", "#aa88cc")

    def _chaossea_reschedule(self, secs):
        if self._chaossea_job:
            self.root.after_cancel(self._chaossea_job)
        self._chaossea_job = self.root.after(
            int(secs * 1000), self._chaossea_tick)

    def _chaossea_tick(self):
        """Placeholder tick - Task 7 replaces this with the real per-room
        loop (room-ready wait, examine mutants, decide targets, move)."""
        self._chaossea_job = None
        if not self._chaossea_on:
            return
        if not (self.conn and self.conn.alive):
            self._chaossea_stop("disconnected"); return
        self._chaossea_reschedule(1.0)

```

- [ ] **Step 4: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 5: Register the command - capitalized-word list, dispatch, help text**

Find (around line 7076-7080):

```python
                    "VN", "Track", "Untrack", "Bot",
                    "Hunt", "Scan", "Gswap", "Run",
                    "Reagents", "Explore", "Agent",
                    "Reminder", "Reminders", "Corpse",
                    "Vskills", "Missionlist", "VNlist")
```

Replace with:

```python
                    "VN", "Track", "Untrack", "Bot",
                    "Hunt", "Scan", "Gswap", "Run",
                    "Reagents", "Explore", "Agent",
                    "Reminder", "Reminders", "Corpse",
                    "Vskills", "Missionlist", "VNlist",
                    "Chaossea")
```

Find (around line 8205-8208):

```python
        elif name == "hunt":
            self.cmd_hunt(arg)
        elif name == "run":
            self.cmd_run(arg)
```

Replace with:

```python
        elif name == "hunt":
            self.cmd_hunt(arg)
        elif name == "chaossea":
            self.cmd_chaossea(arg)
        elif name == "run":
            self.cmd_run(arg)
```

Find the help text block you edited in Task 4 Step 1 (the updated `Hunt | Hunt mapless | Hunt off` entry) and add a new entry directly after it:

```python
  Chaossea | Chaossea off  (Sea of Chaos item hunt: builds a session-local
      fake-room map from directions taken (no roomid - that zone shares one
      vnum and regenerates per visit), examines every mutant it meets
      ('examine mutant'/'mutant 2'/...), fights ones carrying 'a chaotic
      charm' or 'a cube of raw chaos' (whichever you still need), or that
      aggro/block you. Loots via 'get all' + an explicit 'get charm' (bind-
      on-pickup). Retreats ('retreat from the sea') and stops once you have
      both. Deadman trip stops in place - no verified path home.)
```

- [ ] **Step 6: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 7: Verify the command parses without crashing, via a scratch import check**

```bash
python3 -c "
import ast
tree = ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())
names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
for required in ('cmd_chaossea', '_chaossea_start', '_chaossea_stop',
                 '_chaossea_tick', '_chaossea_move', '_chaossea_pick_move',
                 '_chaossea_alloc_room'):
    assert required in names, f'missing {required}'
print('OK')
"
```

Expected output: `OK`

- [ ] **Step 8: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Add Chaossea command scaffolding (start/stop/tick stub, registration, help)"
```

---

### Task 7: Room-ready detection + mutant-count reading for Chaossea

**Files:**
- Modify: `katmud_lib/client.py` (`poll_queue` ~line 5412, new `_chaossea_on_prompt`/`_chaossea_on_room_packet`/`_chaossea_scan_markers`/`_chaossea_enter_room`, `sql_follow_ddd` ~line 1489)

**Interfaces:**
- Consumes: `self._line_markers(spans)` (existing static method, returns `(has_italic, has_underline)`), `self._line_is_party(clean)` (existing).
- Produces: `_chaossea_enter_room(self)` (resets per-room flags, arms the wait), `_chaossea_on_prompt(self)`, `_chaossea_on_room_packet(self)`, `_chaossea_scan_markers(self, spans, clean)`.

This mirrors Bot/Hunt's already-proven room-ready race (`_bot_room_displayed`, "first of prompt/packet wins") exactly, with its own state so Chaossea can run independently of Bot/Hunt (they're mutually exclusive in practice, but there's no reason to couple them).

- [ ] **Step 1: Add the room-ready/marker-scan methods**

Insert directly above `_chaossea_tick` (the placeholder added in Task 6 Step 3 - you'll replace its body in Step 4 below, but add these as new methods above it first):

```python
    def _chaossea_enter_room(self):
        self._chaossea_room_mob = self._chaossea_room_player = False
        self._chaossea_mob_lines = []
        self._chaossea_room_mobs = 0
        self._chaossea_mob_idx = 0
        self._chaossea_examine_buf = []
        self._chaossea_examine_pending = False
        self._chaossea_room_ready = False
        self._chaossea_waiting_room = True
        self._chaossea_reschedule(self._bot_safety_s())

    def _chaossea_on_prompt(self):
        if not self._chaossea_on:
            return
        self._chaossea_room_displayed()

    def _chaossea_on_room_packet(self):
        if self._chaossea_on:
            self._chaossea_room_displayed()

    def _chaossea_room_displayed(self):
        if self._chaossea_waiting_room and not self._chaossea_room_ready:
            self._chaossea_room_ready = True
            self._chaossea_room_mobs = len(self._chaossea_mob_lines)
            self._chaossea_reschedule(0)

    def _chaossea_scan_markers(self, spans, clean):
        ital, under = self._line_markers(spans)
        if ital:
            self._chaossea_room_mob = True
            self._chaossea_mob_lines.append(clean)
        elif under and not self._line_is_party(clean):
            self._chaossea_room_player = True

```

- [ ] **Step 2: Wire the scan/prompt hooks into `poll_queue`**

Find (around line 5457-5462, right next to Bot's equivalent hooks):

```python
                        if self._bot_on or self._run_on:
                            self._scan_pwho(clean)       # seed party whitelist
                            self._resume_scan_line(clean)  # post-kill hold
                        if self._bot_on and self._bot_waiting_room \
                                and not self.in_combat:
                            self._bot_scan_markers(spans, clean)
```

Replace with:

```python
                        if self._bot_on or self._run_on or self._chaossea_on:
                            self._scan_pwho(clean)       # seed party whitelist
                            self._resume_scan_line(clean)  # post-kill hold
                        if self._bot_on and self._bot_waiting_room \
                                and not self.in_combat:
                            self._bot_scan_markers(spans, clean)
                        if self._chaossea_on and self._chaossea_waiting_room \
                                and not self.in_combat:
                            self._chaossea_scan_markers(spans, clean)
```

Find (around line 5475-5479):

```python
                    if kind == "prompt":
                        if self.guild.lower() == "changelings":
                            self.changeling_scan_line(clean)
                        if self._bot_on:
                            self._bot_on_prompt()
```

Replace with:

```python
                    if kind == "prompt":
                        if self.guild.lower() == "changelings":
                            self.changeling_scan_line(clean)
                        if self._bot_on:
                            self._bot_on_prompt()
                        if self._chaossea_on:
                            self._chaossea_on_prompt()
```

- [ ] **Step 3: Wire the room-packet hook into `sql_follow_ddd`**

Find (around line 1500-1506):

```python
        vnum = self._sql_resolve_low5(pe.vnum)   # DDD has no bracket: lift it
        self._sql_cur_vnum = vnum
        self.room_vnum = vnum
        self._bot_on_room_packet()              # react before the map render,
        #                                         so the bot never waits on it
        if pe.exits:
            self.exits = pe.exits
            self.map.update_room(exits=pe.exits)
```

Replace with:

```python
        vnum = self._sql_resolve_low5(pe.vnum)   # DDD has no bracket: lift it
        self._sql_cur_vnum = vnum
        self.room_vnum = vnum
        self._bot_on_room_packet()              # react before the map render,
        self._chaossea_on_room_packet()         # so neither bot waits on it
        if pe.exits:
            self.exits = pe.exits
            self.map.update_room(exits=pe.exits)
```

- [ ] **Step 4: Make the tick call `_chaossea_enter_room` on start, replacing the placeholder body**

Find the `_chaossea_tick` placeholder from Task 6 Step 3:

```python
    def _chaossea_tick(self):
        """Placeholder tick - Task 7 replaces this with the real per-room
        loop (room-ready wait, examine mutants, decide targets, move)."""
        self._chaossea_job = None
        if not self._chaossea_on:
            return
        if not (self.conn and self.conn.alive):
            self._chaossea_stop("disconnected"); return
        self._chaossea_reschedule(1.0)
```

Replace with:

```python
    def _chaossea_tick(self):
        self._chaossea_job = None
        if not self._chaossea_on:
            return
        if not (self.conn and self.conn.alive):
            self._chaossea_stop("disconnected"); return
        if self.deadman_tripped:
            if self.in_combat:
                self._chaossea_reschedule(1.0); return
            self._chaossea_stop(
                "deadman tripped - stopped in place, no verified path home")
            return
        if self.in_combat:
            self._chaossea_reschedule(1.0); return
        if not self._chaossea_waiting_room and not self._chaossea_room_ready:
            self._chaossea_enter_room(); return
        if not self._chaossea_room_ready:
            self._chaossea_reschedule(0.3); return   # still waiting on display
        # Task 8 takes over from here: examine mutants in the room, decide
        # whether to engage each, then move on. For now (this task), just
        # confirm the room-ready race works: log it and roam blindly.
        self._chaossea_waiting_room = False
        d = self._chaossea_pick_move(self.exits)
        if d is None:
            self._chaossea_stop("no exit from here"); return
        self._chaossea_move(d)
        self._chaossea_last_dir = d
        self.send_line(d)
        self._chaossea_enter_room()
```

(Task 8 replaces the "Task 8 takes over" section with the real examine/target loop; this task's goal is only the room-ready plumbing, verified by the room actually advancing.)

- [ ] **Step 5: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 6: Verify the room-ready race logic against the real methods**

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from types import SimpleNamespace
import katmud_lib.client as c

rescheduled = []
f = SimpleNamespace(
    _chaossea_on=True,
    _chaossea_waiting_room=True,
    _chaossea_room_ready=False,
    _chaossea_mob_lines=['A hideous mutant.', 'A withered mutant.'],
    _chaossea_room_mobs=0,
    _chaossea_reschedule=lambda secs: rescheduled.append(secs),
)
# _chaossea_on_prompt/_chaossea_on_room_packet both call self._chaossea_room_displayed()
# internally - stub it as a delegate to the real method so the wrappers' own
# guard logic (self._chaossea_on check) is still exercised, not bypassed
f._chaossea_room_displayed = lambda: c.MudClient._chaossea_room_displayed(f)
# room packet arrives first
c.MudClient._chaossea_on_room_packet(f)
assert f._chaossea_room_ready is True
assert f._chaossea_room_mobs == 2
assert rescheduled == [0]
# a LATER prompt for the same room must not re-trigger (already ready)
c.MudClient._chaossea_on_prompt(f)
assert rescheduled == [0]   # unchanged - no second reschedule(0)
print('OK')
"
```

Expected output: `OK`

- [ ] **Step 7: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Add Chaossea room-ready detection and mutant-count reading"
```

---

### Task 8: Examine each mutant, parse for target items, decide engagement

**Files:**
- Modify: `katmud_lib/client.py` (`poll_queue` line-scan hook, `_chaossea_tick`, new `_chaossea_examine_scan_line`, `_chaossea_next_examine`, `_chaossea_finish_examine`)

**Interfaces:**
- Consumes: `self._chaossea_has_charm`/`self._chaossea_has_cube`, `self._chaossea_room_mobs`, `self._chaossea_mob_idx`.
- Produces: `_chaossea_mob_keyword(self, idx)` -> `"mutant"` for idx 1, `"mutant {idx}"` for idx>1 (every mob in this zone IDs to the single keyword "mutant", per the user; ordinal suffix is the provisional disambiguation for multiple in one room - **flagged in the design doc as needing live confirmation of the MUD's actual ordinal syntax**), `_chaossea_next_examine(self)` (sends the next `examine` or, once all are checked, decides the room's outcome), `_chaossea_examine_scan_line(self, clean)` (accumulates output until the next prompt finalizes it).

- [ ] **Step 1: Add the keyword helper and examine-capture methods**

Insert directly above `_chaossea_enter_room` (added in Task 7 Step 1):

```python
    @staticmethod
    def _chaossea_mob_keyword(idx):
        """Every mob in Sea of Chaos IDs to the single keyword "mutant"
        regardless of its random short description (per the user) - so
        targeting never needs per-mob name parsing, only an ordinal suffix
        to disambiguate more than one in a room. PROVISIONAL: the exact
        ordinal syntax this MUD expects ("mutant 2" vs "2.mutant" etc.) is
        unconfirmed - adjust this one spot once verified live."""
        return "mutant" if idx <= 1 else f"mutant {idx}"

    def _chaossea_wants(self, has_charm, has_cube):
        """Which items are still worth examining/fighting for, given what
        we already have."""
        want = []
        if not has_charm:
            want.append("a chaotic charm")
        if not has_cube:
            want.append("a cube of raw chaos")
        return want

    def _chaossea_next_examine(self):
        """Send the next unchecked mutant's `examine`, or - once every
        mutant in the room has been checked - decide the room is done and
        let the tick move to the next room."""
        self._chaossea_mob_idx += 1
        if self._chaossea_mob_idx > self._chaossea_room_mobs:
            self._chaossea_room_done = True
            self._chaossea_reschedule(0)
            return
        self._chaossea_examine_buf = []
        self._chaossea_examine_pending = True
        kw = self._chaossea_mob_keyword(self._chaossea_mob_idx)
        self.send_line(f"examine {kw}")
        self._chaossea_reschedule(self._bot_safety_s())

    def _chaossea_examine_scan_line(self, clean):
        if self._chaossea_examine_pending:
            self._chaossea_examine_buf.append(clean)

    def _chaossea_finish_examine(self):
        """The prompt after an `examine` landed - decide what to do with
        this mutant from the accumulated text, then either attack it or
        move on to the next one in the room."""
        self._chaossea_examine_pending = False
        text = " ".join(self._chaossea_examine_buf).lower()
        want = self._chaossea_wants(self._chaossea_has_charm,
                                    self._chaossea_has_cube)
        carries = [item for item in want if item in text]
        if carries:
            self._chaossea_engage_mob(self._chaossea_mob_idx, carries)
            return
        self._chaossea_next_examine()

    def _chaossea_engage_mob(self, idx, carrying):
        """Attack the mutant at this ordinal; Task 9 handles the post-kill
        loot + goal update once combat clears (same in_combat gating the
        tick already does for every other bot in this client)."""
        self._chaossea_pending_loot = carrying
        kw = self._chaossea_mob_keyword(idx)
        self.send_line(f"kill {kw}")
        self._chaossea_reschedule(self._hunt_settle_s())

```

- [ ] **Step 2: Add the two new state fields these reference**

In `__init__`, find the Chaossea state block added in Task 6 Step 1 and add two more fields at the end of it:

```python
        self._chaossea_blocked_dir = None  # direction we just tried that
                                           # didn't change rooms (see Task 9)
```

Add directly after that line:

```python
        self._chaossea_room_done = False   # every mutant in this room checked
        self._chaossea_pending_loot = []   # items the just-killed mutant had
```

- [ ] **Step 3: Hook the examine-line scan into `poll_queue`**

Find the line you edited in Task 7 Step 2 (the `if self._chaossea_on and self._chaossea_waiting_room` block) and add a sibling check right after it:

```python
                        if self._chaossea_on and self._chaossea_waiting_room \
                                and not self.in_combat:
                            self._chaossea_scan_markers(spans, clean)
                        if self._chaossea_on and self._chaossea_examine_pending:
                            self._chaossea_examine_scan_line(clean)
```

Find the prompt-handling block you edited in Task 7 Step 2 and extend it:

```python
                    if kind == "prompt":
                        if self.guild.lower() == "changelings":
                            self.changeling_scan_line(clean)
                        if self._bot_on:
                            self._bot_on_prompt()
                        if self._chaossea_on:
                            self._chaossea_on_prompt()
```

Replace the last two lines with:

```python
                        if self._chaossea_on:
                            if self._chaossea_examine_pending:
                                self._chaossea_finish_examine()
                            else:
                                self._chaossea_on_prompt()
```

- [ ] **Step 4: Replace the tick's room-ready section to drive the examine loop**

Find the body of `_chaossea_tick` written in Task 7 Step 4, specifically this tail section:

```python
        if not self._chaossea_room_ready:
            self._chaossea_reschedule(0.3); return   # still waiting on display
        # Task 8 takes over from here: examine mutants in the room, decide
        # whether to engage each, then move on. For now (this task), just
        # confirm the room-ready race works: log it and roam blindly.
        self._chaossea_waiting_room = False
        d = self._chaossea_pick_move(self.exits)
        if d is None:
            self._chaossea_stop("no exit from here"); return
        self._chaossea_move(d)
        self._chaossea_last_dir = d
        self.send_line(d)
        self._chaossea_enter_room()
```

Replace with:

```python
        if not self._chaossea_room_ready:
            self._chaossea_reschedule(0.3); return   # still waiting on display
        self._chaossea_waiting_room = False
        if self._chaossea_has_charm and self._chaossea_has_cube:
            self.send_line("retreat from the sea")
            self._chaossea_stop("got both - retreated"); return
        if self._chaossea_examine_pending:
            self._chaossea_reschedule(0.3); return   # waiting on an examine
        if not self._chaossea_room_done:
            if self._chaossea_mob_idx == 0 and self._chaossea_room_mobs:
                self._chaossea_next_examine(); return
            if self._chaossea_room_mobs == 0:
                self._chaossea_room_done = True
            else:
                self._chaossea_reschedule(0.3); return
        # room fully checked (or empty) and nothing to fight here - move on
        self._chaossea_room_done = False
        d = self._chaossea_pick_move(self.exits)
        if d is None:
            self._chaossea_stop("no exit from here"); return
        self._chaossea_move(d)
        self._chaossea_last_dir = d
        self.send_line(d)
        self._chaossea_enter_room()
```

- [ ] **Step 5: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 6: Verify the examine-decision logic against the real `_chaossea_finish_examine`**

This exercises `_chaossea_wants` and the carries-check together, through the actual method, by observing which of the two stand-in callables (`_chaossea_engage_mob` vs `_chaossea_next_examine`) it invokes:

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from types import SimpleNamespace
import katmud_lib.client as c

def make(text, has_charm, has_cube, calls):
    return SimpleNamespace(
        _chaossea_examine_pending=True,
        _chaossea_examine_buf=[text],
        _chaossea_has_charm=has_charm,
        _chaossea_has_cube=has_cube,
        _chaossea_mob_idx=1,
        _chaossea_engage_mob=lambda idx, carrying: calls.append(('engage', idx, carrying)),
        _chaossea_next_examine=lambda: calls.append(('next',)),
        # _chaossea_finish_examine calls self._chaossea_wants(...) internally -
        # stub it as a delegate to the real (self-independent) method
        _chaossea_wants=lambda hc, hb: c.MudClient._chaossea_wants(None, hc, hb),
    )

# carries the charm, we still need it -> engage
calls = []
f = make('A withered mutant carries a chaotic charm.', False, False, calls)
c.MudClient._chaossea_finish_examine(f)
assert f._chaossea_examine_pending is False
assert calls == [('engage', 1, ['a chaotic charm'])], calls

# carries the cube, but we already have one -> not a target, move on
calls = []
f = make('A hulking mutant clutches a cube of raw chaos.', False, True, calls)
c.MudClient._chaossea_finish_examine(f)
assert calls == [('next',)], calls

# carries neither -> ignore, move on
calls = []
f = make('A mutant snarls, hands empty.', False, False, calls)
c.MudClient._chaossea_finish_examine(f)
assert calls == [('next',)], calls

# carries both items mentioned in one description, need both -> both flagged
calls = []
f = make('It holds a chaotic charm and a cube of raw chaos.', False, False, calls)
c.MudClient._chaossea_finish_examine(f)
assert calls == [('engage', 1, ['a chaotic charm', 'a cube of raw chaos'])], calls
print('OK')
"
```

Expected output: `OK`

- [ ] **Step 7: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Add Chaossea examine-mutant loop: capture, parse for target items, decide engagement"
```

---

### Task 9: Post-kill loot and goal tracking

**Files:**
- Modify: `katmud_lib/client.py` (`_chaossea_tick`, new `_chaossea_after_kill`)

**Interfaces:**
- Consumes: `self._chaossea_pending_loot` (from Task 8's `_chaossea_engage_mob`), `self.in_combat`.
- Produces: `_chaossea_after_kill(self)` (called once a Chaossea-initiated fight clears), `self._chaossea_was_fighting` (bool), updates `self._chaossea_has_charm`/`_has_cube`. Changes the sentinel for `self._chaossea_pending_loot` from `[]` (set in Task 6/8) to `None` - see Step 1.

- [ ] **Step 1: Fix the pending-loot sentinel**

`self._chaossea_pending_loot` needs three distinct states: "not currently fighting anything Chaossea started" (must be distinguishable from) "just killed one that wanted nothing" (`[]`) and "just killed one carrying specific items" (a non-empty list). `[]` can't serve as both "not fighting" and "fought one with nothing", so the not-fighting sentinel must be `None`.

In `__init__`'s Chaossea block (Task 6 Step 1), find:

```python
        self._chaossea_room_done = False   # every mutant in this room checked
        self._chaossea_pending_loot = []   # items the just-killed mutant had
```

Replace with:

```python
        self._chaossea_room_done = False   # every mutant in this room checked
        self._chaossea_pending_loot = None  # items the just-killed mutant had
                                            # (None = not currently fighting
                                            # one - distinct from [], "fought
                                            # one that carried nothing we need")
        self._chaossea_was_fighting = False
```

- [ ] **Step 2: Add the post-kill handler**

Insert directly above `_chaossea_tick`:

```python
    def _chaossea_after_kill(self):
        """A Chaossea-initiated fight just cleared (in_combat went True then
        False while self._chaossea_pending_loot was set). Loot, update the
        goal state, then resume the examine loop for any remaining mutants
        in this room."""
        self.send_line("get all")          # cube isn't bind-on-pickup
        if "a chaotic charm" in self._chaossea_pending_loot:
            self.send_line("get charm")    # bind-on-pickup, get-all skips it
            self._chaossea_has_charm = True
        if "a cube of raw chaos" in self._chaossea_pending_loot:
            self._chaossea_has_cube = True
        self._chaossea_pending_loot = None
        self._chaossea_next_examine()

```

- [ ] **Step 3: Detect the Chaossea fight clearing, from `_chaossea_tick`**

Find, in `_chaossea_tick` (from Task 8 Step 4), the top section:

```python
        if self.in_combat:
            self._chaossea_reschedule(1.0); return
```

Replace with:

```python
        if self.in_combat:
            self._chaossea_was_fighting = True
            self._chaossea_reschedule(1.0); return
        if self._chaossea_was_fighting:
            self._chaossea_was_fighting = False
            if self._chaossea_pending_loot is not None:
                self._chaossea_after_kill(); return
```

- [ ] **Step 4: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 5: Verify `_chaossea_after_kill` against the real method**

The fight-clear *detection* (Step 3's `if self.in_combat` / `if self._chaossea_was_fighting` branch) is four lines embedded directly in `_chaossea_tick`, which has many unrelated preconditions (connection-alive, deadman, room-ready state) - invoking it meaningfully would require a fake large enough to satisfy all of them, which is disproportionate to what those four lines do. Verify that branch by reading the diff in self-review instead. `_chaossea_after_kill` itself is a standalone method and is fully testable directly:

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from types import SimpleNamespace
import katmud_lib.client as c

sent = []
next_calls = []
f = SimpleNamespace(
    _chaossea_pending_loot=['a chaotic charm'],
    _chaossea_has_charm=False,
    _chaossea_has_cube=False,
    send_line=lambda cmd: sent.append(cmd),
    _chaossea_next_examine=lambda: next_calls.append(1),
)
c.MudClient._chaossea_after_kill(f)
assert sent == ['get all', 'get charm'], sent
assert f._chaossea_has_charm is True
assert f._chaossea_has_cube is False
assert f._chaossea_pending_loot is None   # sentinel correctly cleared
assert next_calls == [1]

# carried only the cube -> no 'get charm', has_cube set
sent.clear(); next_calls.clear()
f2 = SimpleNamespace(
    _chaossea_pending_loot=['a cube of raw chaos'],
    _chaossea_has_charm=False,
    _chaossea_has_cube=False,
    send_line=lambda cmd: sent.append(cmd),
    _chaossea_next_examine=lambda: next_calls.append(1),
)
c.MudClient._chaossea_after_kill(f2)
assert sent == ['get all'], sent
assert f2._chaossea_has_cube is True
print('OK')
"
```

Expected output: `OK`

- [ ] **Step 6: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Add Chaossea post-kill loot and goal tracking"
```

---

### Task 10: Blocking-mob detection (room-unchanged signal, not guessed text)

**Files:**
- Modify: `katmud_lib/client.py` (`_chaossea_tick`'s move section, new `_chaossea_handle_blocked`)

**Interfaces:**
- Consumes: `self.room` (the tracked current-room-name string, already maintained by the existing room/BAD handler and shown in the status bar - `self.tb["room"].configure(text=f"{self.room} [{ex}]")`).
- Produces: `self._chaossea_pre_move_room` (str or `None`), `_chaossea_handle_blocked(self)`.

There is no captured wire text for a blocked-by-a-mob movement message (flagged in the design doc), so this does not pattern-match any guessed string. Instead it compares `self.room` from immediately before a move attempt to its value once the room is "ready" again: if they're identical, nothing was gained from the move - either a wall (a real exit token that doesn't actually lead anywhere yet, common in some MUDs' exit lists) or a mob in the way. Since this check only runs once a room's mutants have ALL already been examined (Task 8's tick only reaches the move section after `_chaossea_room_done`), we already know from that examine pass that none of them carry a needed item - so it's safe to force-kill one without missing a charm/cube. Known limitation, accepted rather than solved (documented in the code comment): two genuinely different rooms that happen to render an identical name would be misread as "blocked" - there's no roomid to rule that out, same root constraint as the rest of this zone's design.

- [ ] **Step 1: Record the pre-move room name and add the blocked handler**

Insert directly above `_chaossea_tick`:

```python
    def _chaossea_handle_blocked(self):
        """The last move didn't change self.room - in this zone that can
        mean either a wall or a mob in the way, and there is no captured
        wire text to tell them apart (see design doc). Since this only
        fires after the room's mutants have ALL already been examined
        (nothing here was confirmed to carry the charm/cube), it's safe to
        force-fight one without missing loot, then retry. Gives up once no
        mutants are left rather than retrying a real wall forever."""
        if self._chaossea_room_mobs > 0:
            self._chaossea_room_mobs -= 1
            self._chaossea_pending_loot = []   # already examined - confirmed
                                               # nothing needed on any of them
            self.send_line("kill mutant")
            self._chaossea_reschedule(self._hunt_settle_s())
            return
        self._chaossea_stop(
            "can't move that way and no mutant left to blame - stuck "
            "(real wall, most likely - needs a live look)")

```

- [ ] **Step 2: Add the state field**

In `__init__`'s Chaossea block (Task 6 Step 1), find the line added in Task 6:

```python
        self._chaossea_blocked_dir = None  # direction we just tried that
                                           # didn't change rooms (see Task 9)
```

Replace it (this field was a placeholder name from the original Task 6 scaffolding and is superseded by the field below - delete the old line and add the new one in its place):

```python
        self._chaossea_pre_move_room = None  # self.room just before a move
                                             # attempt, to detect "nothing
                                             # changed" on the next ready
```

`_chaossea_start` (Task 6 Step 3) resets this same field on every fresh run - find, inside `_chaossea_start`:

```python
        self._chaossea_blocked_dir = None
```

Replace with:

```python
        self._chaossea_pre_move_room = None
```

(Without this second replacement, a second `Chaossea` run after a `Chaossea off`/stop would start with a stale value left over from the previous run, since `__init__` only runs once per client launch but `_chaossea_start` runs every time the command starts.)

- [ ] **Step 3: Check it on room-ready, and set it before sending a move**

Find, in `_chaossea_tick` (the move section at the end, from Task 8 Step 4):

```python
        # room fully checked (or empty) and nothing to fight here - move on
        self._chaossea_room_done = False
        d = self._chaossea_pick_move(self.exits)
        if d is None:
            self._chaossea_stop("no exit from here"); return
        self._chaossea_move(d)
        self._chaossea_last_dir = d
        self.send_line(d)
        self._chaossea_enter_room()
```

Replace with:

```python
        # room fully checked (or empty) and nothing to fight here - move on
        self._chaossea_room_done = False
        d = self._chaossea_pick_move(self.exits)
        if d is None:
            self._chaossea_stop("no exit from here"); return
        self._chaossea_move(d)
        self._chaossea_last_dir = d
        self._chaossea_pre_move_room = self.room
        self.send_line(d)
        self._chaossea_enter_room()
```

Now find, still in `_chaossea_tick`, the line set in Task 8 Step 4:

```python
        self._chaossea_waiting_room = False
        if self._chaossea_has_charm and self._chaossea_has_cube:
```

Replace with:

```python
        self._chaossea_waiting_room = False
        if self._chaossea_pre_move_room is not None:
            unchanged = (self.room == self._chaossea_pre_move_room)
            self._chaossea_pre_move_room = None
            if unchanged:
                self._chaossea_handle_blocked(); return
        if self._chaossea_has_charm and self._chaossea_has_cube:
```

- [ ] **Step 4: Verify syntax**

Run: `python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())"`
Expected: no output.

- [ ] **Step 5: Verify `_chaossea_handle_blocked` against the real method**

The room-unchanged *comparison* (Step 3's `if self._chaossea_pre_move_room is not None` block) is a few lines embedded in `_chaossea_tick` alongside the goal-check/examine-loop logic - same disproportionate-fake situation as Task 9 Step 5; verify it by reading the diff in self-review. `_chaossea_handle_blocked` is a standalone method and is fully testable directly:

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from types import SimpleNamespace
import katmud_lib.client as c

sent = []
stopped = []
rescheduled = []
f = SimpleNamespace(
    _chaossea_room_mobs=2,
    _chaossea_pending_loot=None,
    send_line=lambda cmd: sent.append(cmd),
    _chaossea_reschedule=lambda secs: rescheduled.append(secs),
    _chaossea_stop=lambda why: stopped.append(why),
    _hunt_settle_s=lambda: 1.8,
)
# a mutant is still here -> fight it, don't give up
c.MudClient._chaossea_handle_blocked(f)
assert sent == ['kill mutant'], sent
assert f._chaossea_room_mobs == 1
assert f._chaossea_pending_loot == []   # already examined - nothing needed
assert stopped == [], stopped

# nothing left to blame -> stop rather than loop forever
f2 = SimpleNamespace(
    _chaossea_room_mobs=0,
    _chaossea_pending_loot=None,
    send_line=lambda cmd: sent.append(cmd),
    _chaossea_reschedule=lambda secs: rescheduled.append(secs),
    _chaossea_stop=lambda why: stopped.append(why),
    _hunt_settle_s=lambda: 1.8,
)
c.MudClient._chaossea_handle_blocked(f2)
assert len(stopped) == 1, stopped
print('OK')
"
```

Expected output: `OK`

- [ ] **Step 6: Commit**

```bash
git add katmud_lib/client.py
git commit -m "Add Chaossea blocking-mob detection via room-unchanged signal"
```

---

### Task 11: Final review pass

**Files:** None new - review only.

- [ ] **Step 1: Re-read the full Chaossea + mapless diff against the spec**

```bash
git diff 25fb36b..HEAD -- katmud_lib/client.py muds/3s/mud.json muds/3k/mud.json
```

Check against `docs/superpowers/specs/2026-06-20-mapless-wander-and-chaossea-design.md` section by section: mapless flag + command parsing (Part 1), temp-map graph + reciprocal edges (Part 2 temp-map), per-room examine/target/loot loop (Part 2 per-room loop), stop conditions (goal met / manual off / deadman). Confirm every section has a corresponding task above. List anything missing.

- [ ] **Step 2: Confirm the two flagged provisional items are clearly surfaced, not silently assumed correct**

Grep for the word "PROVISIONAL" and "provisional" across the touched code:

```bash
grep -n -i "provisional" katmud_lib/client.py
```

Expected: at least the `_chaossea_mob_keyword` docstring (Task 8) should show up, flagging the ordinal-syntax guess. `_chaossea_handle_blocked` (Task 10) should describe a `self.room`-unchanged comparison, not a guessed message string - confirm no code anywhere does a literal text match for a "you can't go that way, blocked by" style message (since no such text was ever captured).

- [ ] **Step 3: Final full-file syntax check**

```bash
python -c "import ast; ast.parse(open('katmud_lib/client.py', encoding='utf-8').read())" && echo OK
python -c "import json; json.load(open('muds/3s/mud.json')); json.load(open('muds/3k/mud.json'))" && echo OK
```

Expected: `OK` printed twice.

- [ ] **Step 4: Summarize remaining live-only unknowns to the user**

In your final message to the user (not a commit), list explicitly: (1) the exact `examine mutant [N]` output format and real ordinal syntax for multiple mutants is unverified - first live Chaossea room with 2+ mutants will confirm or break it; (2) the blocking-mob case is a `self.room`-unchanged behavioral signal, not a verified message-text match, because no blocked-movement example was ever captured - it can misfire if two different rooms happen to render the same name; (3) mapless Bot/Hunt is logically complete but only exercised via scratch scripts, not a live connection - the user's post-dinner test is the first real run.

No commit for this step - it's a verification/reporting step only.

---

## Post-implementation: update memory

After all 11 tasks are committed and the final review (Task 11) is done, update the project memory files (outside this plan's git commits - these are `~/.claude` memory files, not part of the repo):

- Update `viking-vrest-resume.md` or add a new memory entry summarizing: mapless Bot/Hunt shipped (`Bot mapless`/`Hunt mapless`), Chaossea shipped (temp-map + examine/target/loot/retreat), both built but **not yet live-tested** - flag clearly as pending the user's post-dinner run and first real Sea of Chaos visit, with the two provisional items (examine output format, blocking-mob handling) named explicitly so a future session knows exactly what to verify first.
