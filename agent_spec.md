# In-Game Agent Spec — autonomous assistant, guild-trained

## Status

DESIGN ONLY — approved for spec on 2026-06-16, build next session. Decisions
locked with the user:

- **Shape:** one new *autonomous module* with its own tick loop and its own
  move / fight / heal / upkeep logic (NOT a thin coordinator that just calls
  Bot/Hunt — it owns the loop, though it reuses their helpers where they
  exist).
- **Guild training:** per-guild *Python hook module* (`agent_on_low_hp`,
  `agent_choose_attack`, `pre_fight`, …), the same discipline as
  `necro.py`/`blade.py`/`viking.py`. A declarative default covers guilds with
  no hooks yet.
- **Scope of first build:** TBD at build time — likely scaffold the framework +
  one guild end-to-end, unit-test the pure helpers, then a supervised live run
  before rolling to other guilds (the pattern that worked for the marker bot
  and the FFF vitals rollout).

---

## Context / Intent

The client already has four single-purpose automation engines, all in
`client.py`:

- **Explore** (`_explore_*`, `cmd_explore`) — auto-charts an area's open stubs
  by riding the charting gate; pauses in combat, stops on deadman.
- **Bot** (`_bot_*`, mode "aggro") — roams a charted area, lets aggros bite,
  the MUD's auto-attack + kill trigger fight, then moves on.
- **Hunt** (`_bot_*`, mode "hunt") — actively `kill`s mobs in each room.
- **Run** (`_run_*`) — walks a fixed imported tintin route, killing whitelisted
  mobs (works on 3k TinMap too, no SQL map needed).

Each is a separate command the user starts/stops by hand, and each does exactly
one thing. The **Agent** is the next layer: a single autonomous assistant the
user turns on and walks away from. It decides *what to do next* — chart unknown
rooms, hunt the area, keep itself alive, re-stock/re-memorize, retreat when in
danger — driven by a per-guild profile so the same engine behaves correctly as
a Necromancer, a Bladesinger, a Viking, etc.

This spec covers **only the Agent subsystem**. It builds on the existing map,
combat-state, deadman, and hook machinery; it does not restructure any of them.
Where the Agent needs a behaviour that Bot/Hunt/Explore already implement, it
should **factor the shared helper out and call it from both**, not copy it.

**This is a 3s-first feature** (it leans on the sqlite map: `area_id`,
`stubs_in_area`, `find_path`). A 3k/TinMap variant is a possible follow-up but
is out of scope here — refuse to start on a non-sqlite backend, same as Explore
does.

---

## What the user gets

A capitalized client command `Agent` (registered like `Bot`/`Explore` in the
`process_input` capitalized-command list and routed in `client_command`):

```
Agent              -> status: mode, current goal, area, what it's doing
Agent on           -> start the assistant in the current area
Agent hunt         -> start, but only hunt (skip charting)        [optional verb]
Agent explore      -> start, but only chart (skip hunting)        [optional verb]
Agent off | stop   -> stop (also halted by #stop and disconnect)
Agent debug        -> per-decision report (like `Bot debug`)
```

`Agent on` with no sub-verb runs the **full goal stack** for the active guild:
explore → hunt → upkeep → survive, prioritised each tick (see Decision loop).

---

## Architecture

### Where the code lives

- **Engine:** `client.py`, `_agent_*` methods + `cmd_agent`, mirroring the
  `_bot_*` layout. State block in `__init__` next to the Explore/Bot blocks.
  Self-rescheduling on `self.root.after` — **no separate thread** (every other
  engine here runs on the Tk main loop; combat state, vitals, and the map are
  all touched from there).
- **Guild profiles:** a new dispatch resolved by `self.guild.lower()`. Two-tier:
  1. A **declarative default** (a plain dict of settings, below) so every guild
     works out of the box at a basic level.
  2. An optional **per-guild hook module** providing `agent_*` functions that
     override/extend the default. To respect guild discipline (necro spec 2.4
     — "everything necro-specific lives here"), these hooks live **in the
     existing guild module** (`necro.py`, `blade.py`, …). Guilds with no module
     fall back entirely to the declarative default.

### Guild-hook dispatch

Add a resolver alongside `call_hook`:

```python
GUILD_AGENT_MODULES = {
    "necromancers": necro,
    "bladesingers": blade,
    "vikings":      viking,
    # others added as their modules grow agent_* functions
}

def agent_hook(self, name, *args, default=None):
    """Call agent_<name> on the active guild's module if it defines one,
    else return `default`. Mirrors call_hook but guild-scoped + falls back
    to a value instead of None so callers can supply a default behaviour."""
    mod = GUILD_AGENT_MODULES.get(self.guild.lower())
    fn = getattr(mod, f"agent_{name}", None) if mod else None
    if fn is None:
        return default
    try:
        return fn(self, *args)
    except Exception as e:
        self.write_local(f"agent_{name} error: {e}", "#cc6666")
        return default
```

Note `agent_hook` returns `default` on missing/erroring hook (unlike
`call_hook`'s `None`), so each call site can name the fallback inline:

```python
keyword = self.agent_hook("choose_attack", room, default=self._bot_kill_keyword(...))
```

This is intentionally **guild-scoped and separate** from the global
`katmud_scripts.py` `call_hook` — that file is the user's personal scripting
surface; guild agent logic is shipped code that travels with the guild.

### The hook protocol (per-guild `agent_*` functions)

All take the client `c` as first arg (same convention as `call_hook`). All are
**optional** — omit one and the declarative default applies. Pure where
possible (return a decision; let the engine send it) so they unit-test like the
existing `parse_*` functions.

| Hook | Signature | Returns | Default behaviour |
|---|---|---|---|
| `agent_choose_attack` | `(c, room)` | command string, or `None` to skip | `kill <keyword>` from the italic mob marker (`_keyword_from_mob_line`), else `autoattack_command` |
| `agent_on_low_hp` | `(c, pct)` | command(s) to run (str or list), or `None` | profile `heal_command` if any, else nothing |
| `agent_should_flee` | `(c)` | `True`/`False` | `hp_pct < profile["flee_pct"]` |
| `agent_flee` | `(c)` | command(s) | profile `flee_command` (default `"flee"`) |
| `agent_pre_fight` | `(c, room)` | command(s) or `None` | profile `buffs` not yet up |
| `agent_post_kill` | `(c)` | command(s) or `None` | profile `refresh_command` (e.g. `glance`); harvest/corpse handling is already trigger-driven per guild — don't duplicate it here |
| `agent_upkeep` | `(c)` | command(s) or `None`, and/or a goal request | re-memorize / re-stock when a tracked resource is low (necro: powers/reagents via the Track tracker); default: nothing |
| `agent_is_safe_target` | `(c, room, keyword)` | `True`/`False` | always True (lets a guild veto e.g. a too-tough mob) |

Hooks that need live state read it off `c`: `c.vitals` (`hp`,`hpmax`,`sp`,
`spmax`,`np`,`enemy`,…), `c.vitals_max`, `c.in_combat`, `c.deadman_tripped`,
`c.tracked` (necro Track), `c.guild`, `c.setting(...)`. They act by returning
commands; only return-values are sent, so a hook can't accidentally bypass the
deadman gate.

### Declarative profile (the default "training")

Lives in the guild JSON `settings` (cascade), same place as
`autoattack_command`. Keys (all optional, sensible fallbacks):

```jsonc
{
  "agent_heal_command":    "",          // "" = no self-heal
  "agent_heal_pct":        50,          // run heal below this hp%
  "agent_flee_command":    "flee",
  "agent_flee_pct":        25,          // bail below this hp%
  "agent_buffs":           [],          // cast-once-on-start / when missing
  "agent_rest_command":    "",          // out-of-combat regen (e.g. "rest")
  "agent_rest_until_pct":  80,          // rest until hp/sp back to here
  "agent_refresh_command": "glance",    // post-kill room re-read (reuse bot's)
  "agent_autoattack":      ""           // falls back to autoattack_command
}
```

Add these to every guild JSON's `settings` in one pass (with a `_template` doc
line), exactly like the `autoattack_command` rollout — empty by default, the
user fills them in per guild over time.

---

## Decision loop (`_agent_tick`)

Self-rescheduling via `root.after`, like `_bot_tick`. Priority order — first
applicable goal wins each tick:

1. **Disconnected** → stop.
2. **Deadman tripped** → finish any fight, then `_agent_home` (re-pathfind to
   the start room each step via `_raw_send`, the sanctioned bounded exception —
   reuse/share `_bot_home_step`). Stop when home.
3. **In combat** → don't move. Run survival checks:
   - `hp_pct < flee_pct` (or `agent_should_flee`) → `agent_flee` / flee_command,
     then enter a brief "fled" cooldown and re-assess the new room.
   - `hp_pct < heal_pct` (or `agent_on_low_hp`) → run heal, stay put.
   - else let the MUD's auto-attack work; poll.
4. **Just killed something** (combat just ended) → `agent_post_kill`
   (refresh/glance), wait `agent_postcombat_ms`, re-look the room and re-assess
   (clear multi-mob rooms — same as Hunt's recheck).
5. **Hurt but safe** (out of combat, `hp/sp` below `rest_until_pct`) →
   `agent_rest_command` until recovered (or interrupted by an aggro). Skip if
   no rest command configured.
6. **Upkeep due** → `agent_upkeep` (re-memorize powers, re-stock reagents,
   re-buff). For necros this reads the Track tracker; if a resource is dry and
   re-memorizing needs a guild hall, the upkeep hook may *request a travel
   goal* (path to a landmark) — keep this hook-driven, don't hardcode locations.
7. **Room has a mob** (italic marker) and hunting enabled → `agent_pre_fight`
   then `agent_choose_attack`; gate on `agent_is_safe_target`; cede rooms with
   a non-party player (reuse `_line_is_party` + the pwho whitelist).
8. **Unknown stubs in area** and exploring enabled → take one charting step
   (reuse the Explore machinery: ride the chart gate, `stubs_in_area`, probe).
9. **Area clear & charted** → roam to a random charted same-area exit (reuse
   `_bot_pick_move`), or stop if `Agent` was started in a single-goal mode that
   is complete.

`Agent debug` prints, per tick, the goal chosen and why (e.g.
`[agent] goal=hunt mob=scientist hp=83%`), mirroring `Bot debug`'s wire/client
report.

### Combat & marker reuse

The Agent reads the **same room-presence markers** the bot rewrite uses:
`_line_markers(spans)` → (has_italic mob, has_underline player), room-ready on
first of GA-prompt or DDD packet, keyword from the italic line via
`_keyword_from_mob_line`. **Factor the bot's marker/assess helpers so the Agent
shares them** rather than forking a second copy (see [[area-roam-bot]] — that
machinery is the hard-won part; don't reinvent it).

---

## Safety / non-negotiables

- **Deadman is law.** The Agent does NOT keep deadman alive. When it trips, the
  Agent does the bounded walk-home and stops — never continuous play. Normal
  sends go through `send_line` (deadman-gated); only the walk-home uses
  `_raw_send`. (See the `last_manual` mistake noted in [[area-roam-bot]] — do
  NOT bump it to dodge deadman.)
- **Trigger recursion guard interplay:** the Agent fires commands via
  `process_input`/`send_line`, *not* manual, so it does NOT reset the new
  trigger runaway counter (`TRIGGER_RECURSION_LIMIT`, client.py). Good — an
  agent-driven loop should still be caught by the same guard. Keep the Agent's
  own actions paced (≥ the existing settle delays) so it never machine-guns a
  command faster than the wire settles.
- **Stops on:** `Agent off`/`stop`, `#stop`, disconnect, area drift with no
  same-area exit, and (configurable) on flee with no escape.
- **Combat-stall timeout:** if `in_combat` never clears within
  `agent_combat_timeout_ms`, flee/disengage and re-assess (a follow-up the bot
  doesn't have yet — worth adding here since the Agent runs unsupervised).
- Requires sqlite backend + charted area (for explore/hunt goals); refuse
  otherwise with the same message Explore uses.

---

## Settings summary (cascade)

Engine pacing (new, with `_bot_*` defaults as the starting point):
`agent_postcombat_ms` (2500), `agent_combat_timeout_ms` (30000),
`agent_move_ms` (1500), `agent_tick_ms` (400 idle poll). Plus the declarative
profile keys above. Reuse `bot_party_whitelist` and the pwho roster rather than
a second whitelist.

---

## Suggested build order

1. **Scaffold:** `_agent_*` state block, `cmd_agent`, command registration,
   `Agent`/`Agent off`/`Agent debug`, status report. No behaviour yet — just
   start/stop and a tick that logs its chosen goal.
2. **Factor shared helpers** out of `_bot_*` (marker read, assess, pick-move,
   home-step) so Agent and Bot/Hunt call one copy.
3. **Declarative profile + `agent_hook` dispatcher.** Add the profile keys to
   guild JSONs (`_template` documented). Default behaviours wired through
   `agent_hook(..., default=...)`.
4. **Goal loop** in priority order: explore step → hunt → post-kill →
   survive(flee/heal) → rest → home-on-deadman. Unit-test the pure decision
   helpers (goal selection given a synthetic state; profile resolution).
5. **One guild end-to-end** (pick with user — Necromancer is the richest, has
   the Track tracker for upkeep): add `agent_*` hooks to `necro.py`
   (`agent_choose_attack`, `agent_on_low_hp`, `agent_upkeep` reading powers/
   reagents). Supervised live run.
6. **Roll out** profiles/hooks to remaining guilds as the user trains them,
   reusing the necro pattern (the per-guild workflow from [[guild-mip-method]]:
   capture first, trust the wire, validate vs. live).

---

## Open questions for build time

- **Upkeep travel:** when a necro is out of memorized powers and must return to
  a guild hall to re-memorize, should the Agent path there and back
  automatically (needs a per-guild "home" landmark), or just stop and tell the
  user? (Leaning: hook returns a travel goal if a landmark is set, else stop.)
- **Loot / corpse handling:** post-kill harvest is already trigger-driven per
  guild ([[post-kill-and-psummon]]). Confirm the Agent should leave that to the
  triggers and only issue the `refresh_command`.
- **Multi-goal arbitration:** is the fixed priority order above right, or does
  the user want explore-first-then-hunt as distinct phases (chart the *whole*
  area, then sweep) rather than interleaved per room?
- **Party play:** for now the Agent cedes non-party-player rooms like the bot.
  Any group-coordination behaviour (follow a leader, assist) is out of scope
  until asked.

See [[area-roam-bot]] (the marker/assess/home machinery to reuse),
[[necro-build-status]] / [[blade-build-status]] (guild hook hosts),
[[gswap-status]] (guild-module reload on switch — the Agent must re-resolve its
profile on `Gswap`), and `mapping_redesign_spec.md` (the sqlite map APIs:
`find_path`, `stubs_in_area`, `neighborhood`, `area_id`).
