# KatMUD command reference

Every command the client itself recognizes. Anything not listed here is
sent straight to the MUD.

## How the client decides a line is for it

`process_input` (`katmud_lib/client.py`) checks each line in this order:

1. **Leading `#`** — a client command. Always works.
2. **Leading separator** (`;` by default) — the rest of the line is one
   speedwalk. See [Input forms](#input-forms).
3. **Alias expansion** — first word matched against the alias table.
4. **A capitalised bare word** from the client-command list below, or a
   guild macro name. `Bot` is the client; `bot` goes to the MUD. This is
   deliberate so a MUD exit like `go home` never collides with the
   `Go <landmark>` client command.
5. Otherwise: sent to the MUD (via the sqlite special-exit table if it is
   a single-word move with a charted `Mapfix` command).

**Bare-word commands** (also accept a `#` prefix, lowercase):

`Go` `Landmark` `Mapfix` `Chart` `Maproom` `Maprerate` `Mapnote`
`Maplink` `Mapunlink` `Mapcheck` `Mapwipe` `Toggle` `Toggles` `VN`
`Track` `Untrack` `Bot` `Hunt` `Scan` `Gswap` `Run` `Reagents`
`Explore` `Agent` `Reminder` `Reminders` `Corpse` `Vskills`
`Missionlist` `VNlist` `Chaossea` — plus any guild macro name.

**Everything else is `#`-only.** `#stop`, `#map`, `#maploc`, `#dmg`,
`#var`, `#mob`, `#log` and the whole config/debug set must be typed with
the `#`.

---

## Connection & configuration

| Command | What it does |
|---|---|
| `#connect [host [port]]` | Connect; optional host/port override the profile. |
| `#disconnect` | Drop the connection. |
| `#reload` | Reload the config cascade **and** `katmud_scripts.py`. |
| `#mip` | Re-send the MIP handshake (clears `mip_sent`). |
| `#guild` | Show the active guild and its layer file path. |
| `Gswap <guild>` | Switch the active guild live — reloads the cascade, guild hooks, vitals, and fires the layer's `on_activate` batch. No relaunch. |
| `Gswap` | List available guild layers for this mud. |
| `#deadman <minutes\|off>` | Idle cutoff: after N minutes with no manual input, automation stops and non-manual sends are blocked. Persisted. |
| `#separator <char>` | Change the command separator (default `;`). Single non-alphanumeric character, not `#`. Doubled = literal. |
| `#histmin <n>` | Only keep commands longer than *n* characters in the input history. |
| `#tellsound <path.wav\|beep\|off>` | Sound played on an incoming tell. |
| `#help` | Print the built-in command help in the client. Settings > Command Help... shows the same text in a detached, scrollable panel. |

## Aliases, triggers, gags

| Command | What it does |
|---|---|
| `#alias <name> = <cmds>` | Create/replace an alias in the **character** layer. Body may use `%1`–`%9` and `%*`. |
| `#alias <name>` | Delete that alias. |
| `#aliases` | List all aliases with the layer each came from. |
| `#trigger /<regex>/ = <cmds>` | Create/replace a trigger (character layer). |
| `#trigger /<regex>/` | Delete that trigger. |
| `#triggers` | List all triggers with layer, sound, and party mode. |
| `#trigmode /<regex>/ = party\|noparty\|always` | Restrict a trigger to party or solo play. |
| `#trigsound /<regex>/ = <path.wav\|beep\|off>` | Attach a sound to a trigger (creates a command-less trigger if none exists). |
| `#party [on\|off]` | Set/toggle party mode — decides which `trigmode` triggers fire. |
| `#gag /<pattern>/` | Toggle a gag: matching output lines are hidden. |
| `#gags` | List gags. |
| `#chatgag /<pattern>/` | Toggle a gag that only filters the chat/tell pane. |
| `#chatgags` | List chat gags. |
| `#clearchat` / `#chatclear` | Clear the chat/tell pane. |
| `#keys` | List keybindings and their source layer. |

**Trigger loop guards.** A trigger firing in a runaway loop is
auto-suppressed; a flood of >25 trigger fires in 3s pauses *all* triggers
until `#reload`. A trigger's JSON may set `"cooldown": <secs>` to block
fast refire, or `"recursion_limit": <n>` (`0` = unlimited) to allow a
MUD-forced loop that many times — such a trigger is also exempt from the
flood breaker.

## Automation — roaming and route bots

| Command | What it does |
|---|---|
| `Bot` | Roam the current room's **charted area**, fighting aggro mobs as they engage, then moving on. Needs the sqlite map, autocombat, and the two room-marker asets (see `#markers`). Empty rooms are skipped instantly; rooms holding a non-party player are ceded. |
| `Bot mapless` | Same, but with no map requirement — roams by direction only, for zones that can't be charted (e.g. every room sharing one vnum). No area-left stop; a deadman trip stops in place rather than walking home. |
| `Bot off` | Stop. |
| `Hunt` / `Hunt mapless` | Like `Bot` but **actively attacks** any mob it finds. Uses the guild setting `autoattack_command` if set (a `{t}` token is replaced with the mob keyword from the italic marker line), else `kill <keyword>`. Skips non-party players (party = `pwho` roster + setting `bot_party_whitelist`). Biases movement toward the 3s minimap's mob cells when charted. `Toggle flat` makes it skip up/down exits. When mapless, `wander_clear_limit` (default 30, `0` = off) replaces `hunt_clear_limit`'s no-combat stop. |
| `Hunt <target>` | Same roam, but only kills `<target>`: sends `kill <target>` in any room with a mob and moves on when the MUD answers "There is no `<target>` here." Overrides a self-targeting `autoattack_command`; a `{t}` one gets the target substituted. Note `hunt_clear_limit` still counts target-less rooms as "no combat" — raise it or set it to `0` for a long targeted sweep. |
| `Hunt <target> <watch>` | As above, but the hunt **stops in place and alerts** when a mob line matching `<watch>` appears (bar in the main pane + tell sound + an ntfy push if `ntfy_topic` is set). Checked on room entry and after every kill (the post-kill glance) — before the attack and before the player-skip, so it never kills past the watch mob or misses one in a ceded room. Matched only against the italic `look_monster` lines, so chat can't false-alarm it. A watch mob that arrives *and leaves* mid-fight is missed. Both keywords are single words. |
| `Hunt <dir> [...]` | Fence the hunt: step `<dir>` first and treat the room you left as the boundary. Combines with the target/watch forms (`Hunt north einherjer eihwaz`). |
| `Hunt mapless <target> [<watch>]` | The mapless roam with the same target/watch parsing. |
| `Hunt debug` / `Bot debug` | Toggle per-room reporting: ready time, trigger, mob/player flags, decision. |
| `Hunt off` / `Bot off` | Stop. |
| `Run <bot> [loop] [<step>]` | Fixed-route path bot from `muds/<mud>/bots/*.json`: walks the route, kills target mobs (whole-line match, or a keyword via `"contains"`), loots via `after_kill`, skips rooms with a non-party player or a `skip_phrase`, then loops or returns to start. `loop` (or `l`) repeats; a bare number starts at that step index. |
| `Run resume` | Restart the last bot from its in-memory step index (survives any stop — deadman, manual, disconnect — until a new run starts or the client closes). |
| `Run` | List available bots, or show progress if one is running. |
| `Run off` | Stop. |
| `Chaossea` | Sea of Chaos runner. Builds a session-local fake-room map from directions taken (that zone shares one vnum and regenerates per visit). Movement always dives a `down` exit on sight, else prefers unexplored directions, else BFS-retraces the temp map to the nearest unexplored room. Examines every mutant it meets, fights ones carrying `a chaotic charm` or `a cube of raw chaos` (whichever you still need) or that aggro/block you, loots via `get all` plus an explicit `get charm` (bind-on-pickup), retreats and stops once you have both. |
| `Chaossea fight` | (`fighting` also accepted.) Kills every mutant it meets and ignores all drops. No stop condition besides deadman or a manual stop. |
| `Chaossea resume` | Resume the previous run. |
| `Chaossea clear` | Wipe the temp map and re-seed from the current room, **without** restarting — mode and found-item progress survive. (Starting `Chaossea` clears it automatically.) |
| `Chaossea off` | Stop. A deadman trip stops either mode in place — there is no verified path home. |
| `Agent [on\|full]` | Autonomous assistant: phased — charts the whole area (`Explore`), then sweeps it (`Hunt`), healing/fleeing via the active guild's profile, with a safety gate (area top class vs. your best kill) before hunting. Deadman walks home, then stops. |
| `Agent hunt` / `Agent explore` | Run that phase only. |
| `Agent` / `Agent status` | Show mode, phase, goal, area, hp and pool percentages. |
| `Agent debug` | Toggle per-tick goal reporting. |
| `Agent off` | Stop. |
| `#stop` | Kill switch: stops `Bot`/`Hunt`, `Run`, `Explore` **and the `Chart <entry>` auto-charter** (they share one flag), `Agent`, a running guild macro, and the script bot (sets the `bot` var to `off`). It cancels any walk in progress. **It does not stop `Chaossea`** — use `Chaossea off`. |

**`Esc`** is the panic key, and it does exactly one thing per press, in
priority order: unfreeze the output pane if PgUp froze it; else stop any
running automation (the same kill switch as `#stop`, so likewise not
`Chaossea`); else, in charting mode, drop the whole queued command buffer
and any in-flight gate wait. Otherwise it just clears the input box.

## Mapping — sqlite backend

The sqlite backend is the current mapping system. Location is proven by
the MUD's room packet.

| Command | What it does |
|---|---|
| `Chart` | Toggle map-building mode. Input is gated one move at a time so you can't outrun the map; `Esc` clears the queue. Without it, "following" mode just tracks you live. |
| `Chart <entry> [<return>] [simple]` | One-shot auto-charter: take `<entry>` into an area, chart every room and complete every stub (bounded to the entry room's rated area), then return to start and report. `simple` = compass exits only; `<return>` handles an asymmetric entry. Pauses in combat, stops on deadman/`#stop` — supervise it. |
| `Chart off` | Stop the **auto-charter** only (`stop`/`halt` also accepted); reports "Chart walk is not running" if it isn't. It does *not* leave manual charting mode — for that use bare `Chart`, `Toggle charting`, or `#map off`. |
| `Explore` | Auto-chart the current area's open stubs: probes each unmapped exit through the charting gate, filling the map outward. Only cardinal/ordinal exits are auto-walked; up/down/enter/out etc. are flagged for manual review. Adjudicable mischarts (two exits to one room; two rooms reaching one room the same direction) are auto-re-charted. Unrated rooms are walked to and rated; overland borders rate area-less and are left. Short hops (≤2 rooms) stay gated so a probe never charts from a stale room; only a jump >2 rooms away drops charting for a fast, arrival-verified walk. Walks back to the starting room when done. Auto-enters Chart mode. Pauses in combat, stops on deadman. |
| `Explore` (while running) | Report stubs left. |
| `Explore off` | Stop. |
| `Mapcheck` | Read-only audit of the current area: mischarts (reverse contradictions, same-direction collisions, dupe-target exits), unrated in-area rooms, and non-cardinal stubs. Changes nothing. |
| `Mapwipe` | Preview a wipe of the current area. |
| `Mapwipe confirm` | **Delete** all rooms and exits of the current area for a clean re-chart. The area name is kept; inbound doors become stubs. Stop `Explore` first. |
| `Maproom` | Show full details for the current room. |
| `Maprerate` | Re-send the rating request for the current room. |
| `Mapnote <text>` / `Mapnote clear` | Attach/clear a note on the current room. |
| `Maplink <dir> [<command>] <destvnum>` | Manually chart an exit. |
| `Mapunlink <dir>` / `Mapunlink <command>` | Remove a charted exit. |
| `Mapfix <dir>` | Show the special-exit record for that direction. |
| `Mapfix <dir> cmd\|setup\|return\|wait <value>` | Set one field of a special exit — the command to send, a prerequisite command, the reverse command, or a delay in whole seconds. An empty value clears the field. A charted `cmd` means typing the bare direction (or hitting the numpad key) sends the sequence instead; a `wait` exit sends the setup, waits, then moves. |
| `Mapfix <dir> relink <vnum\|clear>` | Repoint (or unpoint) that exit's destination. |
| `Mapfix <dir> clear` | Clear the whole special-exit record. |
| `#mapfix ...` | Same command with the `#`; errors out on the legacy backend. |
| `#map` / `#map here` | (args are lowercased, so `#map HERE` works.) Current room: vnum, short description, area (+ author), and every charted exit with its cmd/setup/wait notes. |
| `#map on` / `#map off` | Pause/resume charting. |
| `#map rate` | Re-rate the current room (refreshes the area only, charts nothing). |
| `#maploc` | Print the current vnum and whether it is charted. |
| `#landmark add <tag>` | Save the current (charted) room under `<tag>`. Landmarks are mud-wide, one namespace per map. |
| `#landmark del <tag>` | Remove it. |
| `#landmark` / `#landmark list` | List landmarks. |
| `Landmark ...` | Same, bare-word. |
| `Go <name>` / `#go <name>` | Walk to a landmark. On a Viking character a guild-map coordinate landmark is tried first. |
| `#chartdebug` | Toggle the chart-gate trace (each pump/arrival/edge), so wrong-room edges and stalls are visible. |
| `#markers` | Toggle room mob/player marker debug. Requires `aset look_monster italics` and `aset look_player underline` on the MUD. |

## Mapping — legacy (tintin) backend

Used only when `map_backend` is not `sqlite`. Same command names,
different behaviour.

| Command | What it does |
|---|---|
| `#map here` | Room id, name, area, coder, house flag, plus the JSON patch syntax for a redirect. |
| `#map on` / `#map off` | Enter/leave mapping mode. Mapping auto-engages when you walk off the known map (disable with `settings.auto_mapping false`). |
| `#map rate` | Re-rate the located room. |
| `#map new` | Bootstrap a blank map from the current room. Refuses if a map file already exists. |
| `#maploc` | Show the located room id, or the candidate list if unlocated. |
| `#landmark add <name> [desc]` | Save the located room. |
| `#landmark del <name>` | Delete it. |
| `#record [scope]` | Arm recording of the next observed edge into the given layer scope — `character` (default), `guild`, `mud`, or `global`. You must be located. Refuses on the sqlite backend, pointing at `Mapfix` instead. |
| `#speedruns [filter]` | List known speedrun destinations and landmarks, loaded from the tintin map's extra data. Filters: `shop` `mob` `area` `eq` `misc` `clan` `crafting` `landmark`. |

## Toggles

`Toggle` / `Toggles` (or `Toggle` with no name) lists every switch with
its state; `Toggle <name>` flips one. Switches persist per character —
except `charting`, which always boots off.

| Toggle | Meaning |
|---|---|
| `charting` | Map building (same as `Chart`). |
| `mapdetail` | Room info in the side pane. (`mapdetails` also accepted.) |
| `flat` | `Hunt` skips up/down exits. |
| `necroguard` | Necromancers only: auto con/protection/veil. |
| `blur` | Bladesingers only: auto-fire the kickoff blur before a reset wastes charges. |

The rest are **data-driven** and appear only when the loaded config
defines them: one per distinct `"toggle"` key on a trigger (e.g.
`harvest` on 3k Necromancers), plus the corpse routine's gate if it has
one. Run `Toggle` to see what is actually live for the current
character.

## Loot, tracking, reminders

| Command | What it does |
|---|---|
| `Corpse` | Show the on-kill loot routine: solo and party commands, their source layer, the gating toggle, and which mode is active right now. |
| `Corpse <cmds>` / `Corpse solo <cmds>` | Set the solo routine. |
| `Corpse party <cmds>` | Set the party-mode routine. If unset, party uses the solo routine. |
| `Corpse off` / `Corpse solo off` / `Corpse party off` | Clear it. |
| `Track <name> [low]` | Necromancers: watch a power/reagent count in the info pane, dim-red below `<low>`; counts refresh from `powers`/`gs` readouts. Bladesingers: `Track <skill>` shows GXP still needed to raise it, refreshed from the `skills` readout with spendable GXP live. |
| `Track` | List what is tracked. |
| `Untrack <name>` | Stop tracking it. |
| `Reagents [n]` | Necromancers: send `gs`, then buy each reagent up to `n` (default 999) using the fresh counts, skipping bloodmoss. |
| `Reminder <time> <text>` | Set a reminder, e.g. `Reminder 5m buff wore off`, `Reminder 1h30m reboot soon`. Shared across muds and characters. |
| `Reminder every <time> <text>` | Repeating reminder. |
| `Reminder` / `Reminders` / `Reminder list` | List pending reminders. |
| `Reminder clear <id>` | Cancel one (`del`/`delete`/`cancel`/`rm` also accepted). |
| `Reminder clear` / `Reminder clear all` | Cancel all. |

**Note on `Corpse`:** separate multiple commands with `/`, **not** the
`;` separator — `;` would split the line before this command ever sees
it. `Corpse bury corpse/glance` is stored as `bury corpse;glance`. Saved
to the character layer.

## Guild macros

Any capitalised word matching a key in the cascade's `macros` section
becomes a client command: `<Macro> <item>` runs its steps with `{x}`
replaced by `<item>`, each step waiting for its `wait` phrase before the
next is sent. `<Macro> off` (or `stop`/`halt`) aborts. Only one macro
runs at a time.

Currently shipped: **`Autoregen <item>`** on 3k Bladesingers
(`muds/3k/guilds/bladesingers.json`) — prepare, then inscribe the runes.
Any layer can add more.

## Viking (3s)

| Command | What it does |
|---|---|
| `#viking` | Open the Viking status window. |
| `#vmarks` | List guild-map coordinate landmarks (POIs; needs `mip_map` on). `Go <name>` walks there, or click the Map tab. |
| `VN <id> <start> <dest>` | Run a newbie fetch errand: accept `<id>`, walk to `<start>`, fetch, walk to `<dest>`, submit. |
| `Vskills` | Send `vskills` and **read** it (rather than swallowing it) to refresh the Stats tab's GXP pools and per-skill training costs. Daler and the four pools stream live over MIP; the skill list updates on demand. |
| `Missionlist` | Send `vtrade prices` + `vmission list`, then recommend which missions to accept for the highest total **net** daler (reward minus the market value of the goods delivered), without exceeding warehouse stock or your remaining daily quota. |
| `Missionlist top5` | Ignore the quota; show the best five anyway. |
| `VNlist [metric]` | Send `vtrade prices` + `vmission newbie` and recommend the highest-`metric` newbie errands up to the remaining daily quota. `metric` is `daler` (default — net value: daler plus the market value of the goods awarded) or one of `timber` `iron` `furs` `fish` `grain` `mead`. Newbie errands cost nothing to accept, so it is a plain top-N pick. |
| `VNlist top5` | Ignore the quota; show the best five. |
| `VNlist clear` | Wipe the Newbie Errands section from the info pane without re-reading the board. |
| `#vperf` | Toggle the Viking BBE perf trace — packet rate, merge vs. render cost, per-tab render breakdown; reports every 5s. |

## Mob database

Auto-filled from `vscan1` reports, keyed by mob name plus the area of the
current room. Any character can read it.

| Command | What it does |
|---|---|
| `Scan <name>` / `#mob <name>` | Search mobs by name; prints area, race, class, and aggression for up to 25 matches. |
| `Scan` / `#mobs` | Report the total mob count. |

## Combat, stats, debugging

| Command | What it does |
|---|---|
| `#dmg` | Damage table: rounds, hits, damage, damage-per-round and per-hit, split overall / noportals / portals. |
| `#dmg reset` | Zero the tracker. |
| `#combat` | Report combat state: `in_combat`, `fff_combat`, current enemy, and the Run bot's engage flags. |
| `#combat clear` (or `reset`/`off`) | Force `in_combat` false when it gets stuck. |
| `#hpbar` | Dump the raw and stripped MIP `I`/`J` hpbar fields plus `stat1`/`stat2`, `blur_portal`, and `last_deltas`. |
| `#var <name> = <value>` | Set a variable (integers are stored as integers). |
| `#var <name>` | Show one variable. |
| `#var` | List all variables. |
| `#mipraw` | Toggle raw MIP packet display. Turning it **off** writes the captured packets to `logs/mip_<timestamp>.log`. |
| `#log [file]` | Toggle logging of **everything** — text, MIP, local client lines, and rating replies. Defaults to the session log for this character. |

## Input forms

**Speedwalk.** A line starting with the separator is one speedwalk:

    ;15el8esr(open gate)q

Grammar is `[count]letter` or `[count](literal command)`. Letters come
from the cascading `speedwalk` section; `global.json` ships
`n s e w u d`, `t`=ne, `v`=se, `z`=sw, `q`=nw, `r`=enter, `o`=out,
`p`=portal, `l`=leave. Anything unmapped goes in parentheses. Counts cap
at 99 and apply to either form. Aliases whose body is a speedwalk work
too.

**Separator.** `;` by default (`#separator` changes it). A doubled
separator is a literal character. A manually typed line cancels any
walk in progress.

**Alias arguments.** `%1`–`%9` are positional; `%*` is everything after
the alias name. Aliases expand recursively, capped at 10 levels.

**Keys and menus.** Numpad keys are bound to movement by default (both
NumLock variants): 8/2/4/6 = n/s/w/e, 7/9/1/3 = nw/ne/sw/se, `-`/`+` =
u/d, 5 = look, 0 = d, Insert = out — rebindable in any layer or via
Tools > Keybindings. `Esc` = panic stop. PgUp/PgDn scroll the output
pane; Up/Down walk the input history. `Ctrl+=` / `Ctrl+-` resize the
font. The Tools menu holds Aliases & Triggers, Keybindings, Viking
Status, Reload cascade, and Switch Character; right-clicking an output
line offers "Trigger from this line...".

## Script hooks

`katmud_scripts.py`, reloaded by `#reload`:

    on_connect(client)          on_line(client, clean)
    on_command(client, cmd)     on_mip(client, tag, data)
    on_vitals(client, v)        on_tick(client)
