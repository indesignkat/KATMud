# Porting the 3s client from MIP to GMCP

Status as of 2026-08-30. Three feeds ported, the rest deferred with reasons.

## Why this is happening

MIP is being phased out. Not immediately, but it is coming, so the port is
scheduled work rather than a nice-to-have.

## The two facts the design rests on

**GMCP and MIP run at the same time.** Confirmed live on 2026-08-30 — the
MIP-fed hpbars stayed populated while the GMCP-fed `Enc:` updated beside
them. This is what makes the port incremental: each feed can be moved and
then checked against the MIP feed still running next to it, with
`Toggle gmcp` off as the rollback at every step.

> An earlier note in this repo claimed the two were *mutually exclusive*.
> That was wrong — a one-data-point inference from a session where MIP went
> silent for an unrelated reason. See "If MIP goes silent" below.

**GMCP is opt-in.** `TelnetFilter(gmcp=False)` refuses option 201 exactly as
the pre-GMCP client did, byte for byte. `Toggle gmcp` turns it on, and
because telnet options are negotiated once at connect, it only takes effect
on the next connect.

## Supersession: how a feed changes hands

Both transports running means anything ported would be handled *twice* —
every chat line rendered twice over. So each ported package declares which
MIP tags it fully replaces:

```python
GMCP_SUPERSEDES = {
    "Comm.Channel.Text": ("CAA", "BAB", "BAG"),
    "Mud.Status":        ("AAF", "AAC"),
    "Room.Info":         ("BAD",),
}
```

A tag is retired by the replacing package's **first actual packet** — never
by `Core.Supported` merely reporting it subscribed.

That distinction was learned the hard way. Subscribed does not mean it will
ever arrive: `Mud.Status` was subscribed in two captures and sent 2 packets
in 4.6 minutes in one, and **zero in 38.6 minutes** in the other. Retiring
`AAF`/`AAC` on subscription alone traded a working MIP feed for one that
never came, and put uptime and reboot permanently at `?` on the first live
run. Waiting for real data costs at most one duplicated line at the moment
of hand-over — far cheaper than losing a feed outright.

`Core.Supported` is still surfaced: if a package we supersede is reported
*un*subscribed, that is worth a warning, since MIP will be carrying it.

Superseded tags are still teed to `#mipraw` and the session log. Only the
*handler* is skipped. Comparing the two raw streams live is the whole
verification strategy, so the data must keep flowing.

**GMCP is queued ahead of MIP** for each network chunk. Both transports
usually carry the same event in one chunk, and a tag is retired on the GMCP
package's first packet — so with MIP queued first, the session's *first*
chat line rendered twice, once from `CAA` and once from `Comm.Channel.Text`,
before the hand-off had happened. Observed live. Ordering GMCP first means
supersession is already in place when the MIP twin is dequeued.
**LIVE-CONFIRMED 2026-08-30**: before the fix, exactly one duplicated line
per session at the hand-off and clean thereafter; after a relaunch, zero.

`#raw` is the GMCP mirror of `#mipraw`: same on/off, same save-to-log.

## What moved

| MIP tag | GMCP | Notes |
|---|---|---|
| `AAF` uptime, `AAC` reboot eta | `Mud.Status` | MIP sent preformatted strings, GMCP sends raw seconds — `_fmt_duration` keeps the topbar reading the same |
| `CAA` chat, `BAB` tell, `BAG` social | `Comm.Channel.Text` | one package for all three |
| `BAD` room | `Room.Info` | stage 1 — synthesizes the BAD string, feeds the existing path. `DDD` deliberately NOT superseded, see below |
| — | `Char.Vitals` | **additive**, supersedes nothing |
| — | `Char.Combat` | **additive** — fixes the psummon leak, below |
| — | `Guild.State` | **additive** — bladesinger NE/SO pools + exact blur/portal % |

`Comm.Channel.Text` is strictly better than what it replaces. The payload is
structured rather than `~`-split, and direction is an explicit `outgoing`
flag instead of `BAB`'s empty-vs-nonempty FLAG convention — which `BAG`
could not express at all, so `mip_social` had to infer direction from
whether the text began with `"you "`. `prefix` is the mud's own rendering
(`"Din@3k tells you:"`, `"Call <Gossip>:"`), so the sentence no longer has to
be reassembled client-side.

`Char.Vitals` is additive on purpose. `FFF` also carries the guild pools,
`enemy`, `round`, and the login/setup triggering, so it has to keep running;
GMCP only sharpens what it already writes. The gain beyond `enc` is
`maxhp`/`maxsp`: `FFF` never sends maxima, so `vitals_max` is a running
high-water guess there, and a maximum that *drops* — lost equipment, a
drained level — can never be observed. GMCP states it outright.

### Room.Info replaces BAD and DDD

Measured on `logs/din-20260830.log`, a walk with both transports running:

| | result |
|---|---|
| `Room.Info.num` == `BAD` vnum | 15/15 |
| exit sets identical | 15/15 |
| rooms seen | **22 `Room.Info` vs 15 `BAD`** — MIP dropped 32% |
| `look`/`glance` | MIP emits a lone `DDD`; GMCP emits **nothing** |
| failed move | neither emits |

The completeness gap and the `look` behaviour are the real prizes. MIP
silently drops rooms during fast movement, and its lone-`DDD`-on-look is the
ambiguity the glance/skip-debt machinery exists to work around — with
`Room.Info`, a room packet means you moved, full stop.

**LIVE-CONFIRMED 2026-08-30**, second walk after a full restart: 20
`Room.Info` against 13 `BAD` (MIP missed 35%, matching the first walk's
32%), synthesized strings matched the real `BAD` name+exits+vnum 13/13, no
client warnings, and chat rendered once. The route also covered **non-walk
movement** — `chaos` (realm transport), `enter`, `vortex` and a multi-step
`lll` alias — and every one produced a `Room.Info`, which had been an open
question for `Go`/fast-travel.

**Implementation: synthesize, don't reimplement.** `gmcp_room_info` rebuilds
the `BAD` and `DDD` strings and calls `mip_room`/`mip_exits`, which dispatch
to `sql_on_bad`/`sql_on_ddd` as before. `sql_reconcile_chart` and
`sql_follow_*` are the subtlest code in the project and they work;
`Room.Info` is a strict superset of what those strings carry, so the
synthesis is lossless and only the input's *quality* changes, not its
format. Replaying all 15 comparable packets from the capture through the
handler reproduced the real `BAD` name, exits and vnum exactly.

One trap: `sql_on_bad` detects the viking biome by sniffing the raw string
for a `[50960]` prefix and skips charting on it. A synthesized string would
have lost that and started charting biome rooms, so the prefix is
reconstructed for that vnum. **Unverified** — no viking GMCP capture exists.

`area` and the exits' destination vnums are captured into
`gmcp_room_area`/`gmcp_exit_dests` but deliberately **not consumed yet**.

#### What `Room.Info.exits` actually means

**GAME FACT (user, 2026-08-30): the exits list is COMPLETE, except that
hidden exits are never included — and every exit from Da Void is hidden.**

That is why rooms 267/268/269 report `exits: {}` while genuinely having
ordinary, reciprocal exits (`267 n -> 268`, `268 s -> 267`, and so on, all
confirmed against the player's own description of the layout). Likewise the
Wickward Path → Krispy Krematorium entrance (`5264 -> 5841`) is absent from
5264's exits — it is hidden. `BAD`/`DDD` omit hidden exits too, so this is
parity, not a regression.

So, precisely:

* a direction **present** in `exits` is a fact — and its value is the real
  destination vnum, or `0` meaning "destination not yet known";
* a direction **absent** from `exits` means *visible exits do not include
  it* — it may still exist as a hidden exit. Absence is **not** evidence
  that a move is impossible.

This kills an earlier proposal of mine outright: gating charting on "the
departure room's exits must name this direction and destination" would have
refused to chart every hidden-exit traversal, including the whole of Da
Void — edges the player confirmed are correct. Exits are a **positive**
source only.

The genuine win, therefore, is the opposite direction: where `exits` names a
destination, that edge can be charted **without walking it**, which is
something `DDD` could never support.

#### Why `DDD` is NOT superseded — a glance answers with nothing

`Room.Info` is a superset of `BAD`+`DDD` **for movement**. It is not a
superset for `look`/`glance`, where the mud sends **nothing at all**. That
was listed above as an advantage, and for charting it is one. For the bots
it was fatal: `glance` is the `Bot`/`Chaossea` room-refresh command, and the
`DDD` it comes back with is the **only** "the room displayed" signal those
bots have — `_chaossea_on_prompt` never fires on 3s, which sends no telnet
GA.

Superseding `DDD` therefore threw the reply away unread. Live, 2026-08-31
(`logs/normal-20260831.log`): after GMCP came on, `Chaossea fight` sent
**11 glances**, **13 `DDD` packets were dropped** as superseded, and exactly
**1 `Room.Info`** arrived — at login, because the bot never moved again.
`_chaossea_room_ready` never flipped, and the tick re-glanced once per
safety window forever. The main map lost the lines between its rooms at the
same time and for the same reason: `sql_follow_ddd` is what pushes exits
into the renderer, and it had stopped running.

So `Room.Info` supersedes `BAD` only, and `gmcp_room_info` synthesizes only
the `BAD` string — the live `DDD` still arrives and still drives
`sql_on_ddd`, so synthesizing one as well would run the follow/chart path
twice per move. GMCP still supplies the untruncated room number, the area,
and per-exit destination vnums; what it does not supply is a reply to a
question the player asks while standing still.

**Supersession is per-CONNECTION.** `_gmcp_superseded` is cleared in
`connect()`. It used to be set once in `__init__` and survive a reconnect,
so a reconnect that never negotiated GMCP — which happens — kept MIP's
chat, room and exit tags retired with nothing replacing them. That is the
"silent MIP" trap in a new form, and it is what hid the takeover from the
session log: the hand-off had happened on an earlier connection.

### Char.Combat fixes the psummon leak

`self.rounds` is zeroed in exactly one place: a kill-text match. Every other
way a fight can end — you flee, you outrun it, it wanders off, someone else
kills it — leaves the counter stale at whatever it reached. **MIP has no
fight-over signal but the kill text itself.**

The next incidental fight therefore begins at "round 12 against a 100%
enemy", and `katmud_scripts.py` rule 1 (`rounds >= 5` and a fresh enemy)
fires a psummon on its *first* round. That is the sprint-home-past-an-
aggro-mob case: a wasted usage and a shadow portal abandoned in a corridor.

`Char.Combat` ends every fight with one empty snapshot — `attacker ""`,
`target ""`, `rounds 0` — whatever ended it. Zeroing `self.rounds` there
closes the leak. From `logs/gmcp_20260830_135925.log`, two aggro orcs on a
sprint home:

```
{ "target": "you", "rounds": 1253, "attacker": "Orc", "attacker_hp": 100 }
{ "target": "",    "rounds": 0,    "attacker": "",    "attacker_hp": 0 }
{ "target": "you", "rounds": 0,    "attacker": "Orc", "attacker_hp": 100 }
{ "target": "",    "rounds": 0,    "attacker": "",    "attacker_hp": 0 }
```

It deliberately does **not** touch `in_combat`. `Char.Combat` reports who is
attacking *you*, so a passive mob you are killing may never appear in it —
clearing `in_combat` here could end a fight that is still going. Zeroing
rounds errs the safe way: a psummon delayed by a few rounds, never one
wrongly fired.

(`rounds` in a live packet is the *attacker's* fight duration, not yours —
hence the 1253 on an orc that had been scrapping long before you arrived.
Only the empty snapshot is used.)

**2026-09-11: this closed the GMCP half only, and the leak stayed open.**
FFF has its own fight-over and fight-start packets, and none of them touched
`self.rounds`. `mip_vitals_composite` does `self.vitals.update(upd)` *before*
the round branch, so that branch's `len(enemy) > 1` guard already sees the
new value — a fight-end `N~0~K~~` (106 of 109 ends in
`logs/din-20260909.log`) was skipped by its own guard, `K~~` with no `N~0`
(the other 3) had nothing to skip, and back-to-back aggro sends no clear
packet at all. The first packet of the next fight carries `K` and `L` but no
`N`, so `on_vitals` read the previous fight's count against a 100% enemy.
Fixed by zeroing `self.rounds` whenever the FFF `K` field *changes*, ahead of
the round branch, so a packet carrying both `K` and `N` still ends on `N`.
FFF is the transport that always runs; `Char.Combat` stays as-is, additive
and covering the passive-mob case. Tests: `FffRoundResetTest`.

**Same day, that zeroing broke the recent-kills pane.** The mud sends its
fight-over packet *before* the killing-blow text — 106 of 106 fight ends in
`logs/din-20260909.log`, 2 to 8 lines ahead — so `track_combat_line` recorded
every kill as `(mob, 0)`; a ~400-round Aurothon Guardian logged as `(0)` while
the topbar's live readout stayed right. Both fight-end handlers now call
`_stash_rounds`, which keeps the count it clears so the kill line can still
read it, and never stashes a zero — so whichever transport clears first keeps
the real number. A *new* enemy still drops both. Tests: `test_kill_rounds.py`.

### Guild.State: pools, and blur timing at full precision

Guild.State has no shared schema — the field names are guild-specific — so
`GMCP_GUILD_POOLS` grows one capture at a time, keyed by the packet's own
`guild` field (singular: `"bladesinger"`, not the client's plural).

Bladesinger maps `nekra` → `gp1` (the prompt's NE) and `sokra` → `gp2` (SO),
the same bars `FFF`'s E/F/G/H fields already feed. Additive for the same
reason as `Char.Vitals`: `FFF` still owns `enemy`, `round` and the
login/setup triggering.

The real gain is `reset`. Auto-blur decides on the recharge percentage, and
the hpbar only reports it as a **whole number** — but one percent of the blur
cycle is **13.5 rounds**. So a kickoff waiting for "70%" could fire up to
thirteen rounds after the true crossing, a quarter of a blur's 48-round
duration. GMCP sends the same quantity exactly (2/27 per round), so both
`bp` and `pp` now prefer the GMCP value when present and fall back to the
hpbar integer on MIP-only.

Charge counts `n/m` are still not in GMCP, so the hpbar remains the source
for those. It is a deliberately mixed feed.

## What has not moved, and why

| Feed | Why it is still on MIP |
|---|---|
| `FFF` guild pools → `Guild.State` | Field names are per-guild. Only 2 of 17 guilds are documented (bladesinger, elemental). Needs a capture per guild. |
| `BBC` mercenary → `Merc.*` | All five packages are declared but have **never been observed**, even in a session where the player's mercenary was active throughout. Subscribing evidently does not backfill existing state — the merc may need dismissing and re-spawning to emit. Same shape as `Mud.Status`. |
| — `Room.Contents` | Already confirmed entry-only, so it cannot replace the post-kill glance apparatus. No win. |
| `BAE` mudlag | Not wanted. `Mud.Status.lag` is deliberately ignored. |
| `CAP` caption, `EEE` reboot notice | Nothing depends on them — see below. |

### CAP and EEE are dead weight

Across every MIP log in `logs/`, the tags that actually fire are:

```
BBE 24590   FFF 4777   BBC 1153   BAE 405   DDD 404
BAD 255     AAF 92     AAC 92     AAA 72    CAA 42
```

`CAP`, `BAB`, `BAG` and `EEE` appear **zero** times. `CAP` only ever set the
window title. They are registered in `mud.json` but have never been seen on
the wire, so they need no GMCP equivalent.

## If MIP goes silent

**Suspect the handshake before suspecting GMCP.** The `3klient` handshake
fires only when an incoming line contains the literal substring `"elcome"`,
which 3s does *not* print on a takeover login. No trigger, no handshake, and
MIP is silently off for the whole session — `#mipraw` shows nothing and every
hpbar reads 0.

This has now produced a false "GMCP broke MIP" conclusion twice.

1. Look for `[MIP handshake sent, pin NNNNN]` in the scrollback.
2. Absent → this bug. Run `#mip` to re-send.
3. Present but still silent → then investigate for real.

## Next steps, in order of value

1. **`Room.Info`** — the biggest win left. Destination vnums per exit and
   untruncated room numbers would retire `mapparse.is_truncated_id` and
   `_sql_migrate_truncated`. Capture both streams side by side first and
   diff them over a walk before switching anything.
2. **`Guild.State` per guild** — start with bladesinger and elemental, the
   two already documented, and add guilds as captures arrive.
3. **`Merc.*`** — needs a mercenary session to capture.

## Room.Refresh and Room.Death (doc update 2026-09-03)

`gmcp.txt` gained two packages. Neither has been seen on this wire yet.

**`Room.Refresh`** is the only package the client *sends*: it re-sends
Room.Info, Room.Contents and Room.Map for the room you are standing in,
whether or not anything changed — a GMCP `look`. Optional
`{ "packages": [...] }` narrows it. Only subscribed packages come back, and
past a couple of requests a second the rest are dropped **without a reply**.

The prize is not Room.Info on demand — it is **Room.Contents on demand**.
Contents is filed above as entry-only and therefore useless against the
post-kill glance apparatus; if it answers a refresh, that line is obsolete
and the glance/skip-debt machinery has a structured replacement.

It does **not** unblock retiring `DDD`. Two things rode on the DDD reply:
the bots' "room displayed" signal, which Refresh addresses, and
`sql_follow_ddd` pushing exits into the renderer, which it does not — that
is what emptied the map's exit lines on 2026-08-31.

It also breaks the invariant the chart path rests on: *a Room.Info means you
moved, full stop*. A refresh reply carries no field marking it solicited, so
a client that reads one as a move can lay a spurious edge or a self-loop.
The asymmetry to build on: while a request is outstanding, default to
"refresh, do not chart" — guessing wrong that way costs one dropped room
that the next move recovers; guessing wrong the other way is map damage.

`#roomrefresh [info|contents|map]` is the probe, deliberately read-only: it
sends the request, prints each returning `Room.*` package with its round-trip
in ms **and its body** (Contents and Map have no handler, so otherwise the
probe would only report that a packet arrived), suppresses charting until the
solicited `Room.Info` lands, and reports the no-reply case explicitly. Open
questions it exists to answer: does Contents reply and with what; is a reply
distinguishable from a move; what is the real latency; and does a hidden-exit
room (5264, Da Void 267/268) still report `exits: {}` on a refresh.

### First run: no reply (2026-09-03)

**`Room.Refresh` is documented but does not answer on this build.** Four
requests across two sessions, every one confirmed sent, every one silent:

| payload | reply |
|---|---|
| `Room.Refresh` | none |
| `Room.Refresh {}` | none |
| `Room.Refresh { "packages": ["Room.Info"] }` | none |
| `Room.Refresh { "packages": ["Room.Info","Room.Contents","Room.Map"] }` | none |

`logs/gmcp_20260903_080250.log` (110 packets) and
`logs/gmcp_20260903_081147.log` (72 packets) contain no `Room.*` at all.

Everything client-side is ruled out. **Outbound GMCP works** — the server
answers our `Core.Supports.Set` with a `Core.Supported` over the same
`send_gmcp` path and the same option 201, so the requests arrive and parse.
**`Room.Info` is subscribed** — the post-login `Core.Supported` says so and
`[GMCP now owns: BAD]` fired on the login room, so the package is live and
sending. Not-subscribed is dead as an explanation; so is payload shape,
across all four forms. The doc landed ahead of the code.

Two things learned on the way that outlive this question:

**The package surface moved, and `Core.Supported` under-reports it.** The
Aug 31 list and the Sep 3 list are both 18 packages with no
`Guild.Settlement`, `Guild.Trade`, `Guild.Voyage`, `Guild.Fleet`,
`Guild.Market`, `Guild.Livestock`, `Guild.City` or `Guild.Roster` — yet all
eight stream constantly, undeclared and undocumented. So `Core.Supported` is
a floor, not the surface: a package's absence from it proves nothing, which
is why `Room.Refresh` not being listed was never evidence either way.

**3s sends two `Core.Supported` packets**, an all-zero one at the password
prompt and the real one after character login.

Two instruments were added chasing this, and both are worth keeping. `gmcp_core_supported` now prints the
**whole** surface at login, subscribed and not — it previously warned only
about the three superseded packages, so a login said nothing at all about
`Room.*`. `#gmcpsub` reprints it without a reconnect, and `#gmcpsend
<payload>` is a raw passthrough so payload variants (`Room.Refresh {}`,
`Room.Refresh { "packages": ["Room.Info"] }`) cost no edit each.

`#roomrefresh` stays as built: it is the instrument that answers this the
moment the mud implements Refresh, and it costs nothing sitting idle. The
mud is under active development on exactly the surface being ported, so
re-run it after any 3s update.

The rate limit is **not** answerable this way — "a couple a second" needs
sub-500ms spacing that typed input will not reach, and under rapid fire a
late reply is attributed to the following request. That one gets answered by
the first feature that sends refreshes, not by the probe.

**`Room.Death`** `{ name, killer, npc, corpse }` — obviously relevant to the
post-kill gate and the corpse cascade, but it is an *event*, not a snapshot:
no dedup, two identical kills send two frames. And it fires for deaths you
did not cause — `killer` empty means **uncredited, not you**, so looting off
it without gating on your own character would loot other players' kills.
Its own capture, its own change.

## The Viking guild feed (2026-09-03)

The whole Viking feed turns out to be on GMCP as structured JSON, under
**eight packages that `Core.Supported` does not declare and `gmcp.txt` does
not document**: `Guild.Settlement`, `Guild.Trade`, `Guild.Voyage`,
`Guild.Fleet`, `Guild.Market`, `Guild.Livestock`, `Guild.City`,
`Guild.Roster` (plus fields on the declared `Guild.State`). Found by
accident while probing `Room.Refresh`. Every mapping below is pinned to
`logs/gmcp_20260903_08*.log`, never to a doc — there is no doc.

This is the same surface `viking.py` decodes out of MIP's `BBE` tag,
including the parts that needed the most machinery: sub-chunk reassembly
(`KEY_NofM`), `merge_tgoods`, `merge_longship`, `merge_vmapl`. GMCP
paginates instead — `"pages": 6, "page": 3` — which is the same idea done
cleanly.

### Synthesize, don't reimplement — again

The tabs read raw BBE strings out of `viking_state` and parse them at render
time, so `viking.gmcp_bbe_values` / `gmcp_bbe_rows` rebuild the BBE string
and let the existing `parse_*` run. No renderer changes, no second decoder,
and the MIP feed stays byte-comparable beside it. Same reasoning as
`Room.Info`, and the same payoff: only the input's *quality* changes.

### Per-KEY supersession

`GMCP_SUPERSEDES` retires a whole MIP tag, which cannot express this: `BBE`
is one tag carrying every Viking feed, and only some of its keys can move.
So `_viking_gmcp_keys` holds the keys GMCP has delivered, and `mip_viking`
drops exactly those from each BBE packet — retired on first real data, as
everywhere else in this port.

**The trap, found before it shipped:** a long BBE value arrives as
`SHIPS_1of3` and is reassembled into `SHIPS`. A filter testing plain names
lets every piece through, and the packet that *completes* the push
overwrites the GMCP value with the MIP one — intermittently, only for values
long enough to chunk. Hence `viking.base_key`, plus a second filter after
`reassemble_chunks` for a push already in flight when the key changed hands.

### THE RULE, and what it holds back

A key is synthesized only when **every** field its parser reads is present
in the GMCP object. One guessed field is worse than none, because the MIP
feed that would have supplied it gets retired.

| Moved | from |
|---|---|
| `SHIPS` | `Guild.Fleet.ships` (paginated) |
| `ROUTES` | `Guild.Trade.routes` (paginated) |
| `LMARKET` | `Guild.Livestock.lmarket_<lineage>` (paginated, one lineage per sweep) |
| `PATROL`, `WEATHER`, `PRODUCTION` | `Guild.City` |
| `SACTIONS` | `Guild.Settlement` |
| `THRALL_FOLLOWER` | `Guild.Roster` |
| `STFX` | `Guild.State.fx` |

Held back, with the field GMCP does not send:

| Key | missing |
|---|---|
| `LONGSHIP`, `VOYAGE` | `crew_traits`, `ship_traits` |
| `CARTS` | `legs` — the multi-stop route plan |
| `FARM` | `shroom_id` (GMCP sends a display name) and the `meta\|weather_mod` header |
| `BQUEUE` | the `used/cap` header |
| `BLOT` | rendered as a raw string; GMCP sends an object |
| `THRALLS` | GMCP's 20 **named** buildings are a different set from `THRALL_BUILDINGS`' 16 positional slots — no Thrall Pen or Brewery, new apiary/armoury/goldsmith/skald_hall/weaponry. Richer, but not a positional swap |

Each needs a side-by-side MIP capture to resolve, not a guess.

`WEATHER` is a small win beyond parity: its third field was documented as
"undecoded (no readout names it)". GMCP calls it `strength`.

### Sweeps are published whole

The paginated feeds are buffered across a sweep and published on the last
page. A half-drawn sweep would flicker the Raids tab between two ships and
eight on every pass, and a sweep that never finishes must not half-replace a
key MIP is no longer feeding. Rows are keyed by identity so a re-sweep
replaces rather than appends, and the replacement is **scoped**: `SHIPS` and
`ROUTES` arrive whole, but the livestock market arrives one lineage at a
time, so replacing everything would leave `LMARKET` holding a single lineage
out of thirteen.

### Per-KEY supersession is still per-CONNECTION

`_viking_gmcp_keys`, `_viking_gmcp_rows` and `_viking_gmcp_sweep` are
cleared in `connect()`, not just `__init__` — the same fix `_gmcp_superseded`
needed and for the same reason. A reconnect that never negotiates GMCP would
otherwise keep SHIPS/ROUTES/LMARKET/PATROL/WEATHER/PRODUCTION/SACTIONS/
THRALL_FOLLOWER/STFX dropped from every BBE packet with nothing replacing
them, and the Viking tabs would go blank and stay blank. The sweep buffer
belongs in the same reset: a sweep half-collected when the link dropped must
not be completed by the next connection's pages.

Caught by review before the live run, not by the wire.
`PerConnectionResetTest` reads `connect()`'s source and guards all four.

`viking_state` deliberately does **not** clear with them — the merged feed is
meant to persist. One visible consequence: reconnect with GMCP on and
`viking_state["LMARKET"]` still holds thirteen lineages while
`_viking_gmcp_rows` is empty, so the first sweep publishes only the lineages
it carried and the Livestock tab shows one lineage until the cycle comes
round. It self-heals within a sweep cycle. Expected, not a bug.

### First live run, 2026-09-03 — clean

`logs/normal-20260903.log` with `logs/mip_20260903_135126.log` and
`logs/gmcp_20260903_135127.log` beside it: the first session with both
transports logged at once, which is what makes everything below provable.

All nine keys handed over and all six MIP tags did too, with no client
warnings:

```
[GMCP now owns viking STFX, PATROL, SACTIONS, LMARKET,
                 THRALL_FOLLOWER, SHIPS, PRODUCTION, WEATHER, ROUTES]
[GMCP now owns: BAD, AAC, AAF, BAB, BAG, CAA]
```

The MIP handshake fired (`[MIP handshake sent, pin 46650]`), so MIP was
live alongside and the comparison is valid. Incidentally this pins the
edge of the takeover-handshake bug: the trigger needs the literal
`"elcome"`, and a *linkdead resume* prints "3Scapes **welcome**s you back
from linkdeath" — so a linkdead login trips the handshake where a plain
takeover does not.

### THE RULE, sharpened: fields the PARSER READS

MIP's `WSTOCK` opens with a `8085;` header that GMCP does not send. That
looks like a held-back key until you read `parse_wstock`, which skips it:
it is a one-field entry and fails the `len(f) not in (3, 4)` check. Lines
455 and 3350 are its only consumers, and neither sees the header.

So the rule is *every field the parser reads*, not every field on the
wire. It is a narrow widening and worth stating exactly, because
"close enough" is the failure it exists to prevent.

**GAME FACT (user, 2026-09-03): that header is the warehouse's maximum
capacity, 8085 being the value with the character's skill bonuses
applied.** (It is not the stock on hand, which summed to 4236 in the same
packet.)

### The WSTOCK reversal — read this before applying the sharpened rule

`WSTOCK` was ported on the reasoning above and then **reverted**, because
the reasoning was right about the code and wrong about the client.

"No consumer reads the header" was verified and true: `parse_wstock`
skips it, and `warehouse_cap` did not read `WSTOCK` at all. What that
check could not see is that the Trade tab *does* display capacity —
`Warehouse  [4023 / 5250]` — sourcing it from `WAREHOUSE_CAPS`, a
hardcoded **base-tier** table that knows nothing about skill bonuses. So
the client was already showing 5250 where the wire was saying 8085, and
the header was not unused-and-irrelevant; it was unused-and-*better*.

`Guild.Warehouse` is `{pages, page, wstock, guild}` with **no capacity
field**, so porting the key would have retired the only source of the
right number and frozen that readout on the wrong one permanently.

Two things follow, and both outlive this key:

1. **`WSTOCK` stays on MIP**, and `warehouse_cap` now prefers the header,
   falling back to the tier table only until the feed arrives. The
   displayed capacity goes 5250 → 8085.
2. **The sharpened rule needs a second half.** "Every field the parser
   reads" is the right test for whether synthesis is *lossless*. It is
   not the test for whether the port is a *win*: a field nothing reads
   may still be the best available source for something the client is
   currently getting wrong elsewhere. Before retiring a MIP tag, ask what
   the wire carries that the client is approximating — not just what the
   parser consumes.

Found only because the user read the real capacity off the tab and
questioned the number. No amount of log-diffing would have surfaced it,
because both transports agreed — the disagreement was between the wire
and a constant.

### What moved in the second increment

| Moved | from | evidence |
|---|---|---|
| `TGOODS` | `Guild.TradeGoods.goods` (paginated) | one full 14-hold, **7211-byte** value assembled from GMCP matched a real MIP `TGOODS` of the same session **byte for byte** |

`WSTOCK` was moved in the same increment and then **moved back** — see
"The WSTOCK reversal" below. It is the most useful mistake in the port
so far.

`TGOODS` is the most valuable key in the port. The MIP feed carries a
**mud-side framing bug** — a 1-byte tag/data offset, reported to the admin
2026-06-19 — and a 7211-byte value chunked across BBE packets is exactly
where that bites. GMCP paginates instead and the bug cannot exist there.

Its sweep shape is its own: **one sweep is one trade hold**, five pages of
goods then a page-6 terminator carrying only `lin`. So the hold is the
replacement group, and holds are emitted in **numeric** order — lexical
would file hold 10 between 1 and 2, and `parse_tgoods` reads them left to
right. `viking.join_rows` exists for that: TGOODS assembles as
`<lin>=<goods>` joined with `|` while every other key is a flat `;` list.

`WSTOCK` keys its rows by page-and-position rather than by good, because a
good is held in several batches at different freshness — three of eggs,
two of fish — and keying by name would collapse them to one. Its `grade`
is genuinely optional: durable goods (gemstones, runestones, ore, wool)
carry none on *either* transport.

### Still held back after this capture

The seven from the first increment were re-checked field by field against
the real packets. Five stay held, and now with the exact reason:

| Key | GMCP source | missing |
|---|---|---|
| `VOYAGE` | `Guild.Voyage.voyage` | `crew_traits`, `ship_traits` |
| `CARTS` | `Guild.Trade.carts` | `legs` — the multi-stop route plan |
| `BQUEUE` | — | no GMCP field in this capture at all |
| `THRALLS` | — | no buildings object in this capture |

Do **not** relax the rule for `LONGSHIP` on the grounds that MIP sent
those four fields empty this session (`10|Gray Dagger|4|raiding|
Waterford|5299|35|0|0|||||the Wave-Tamer|61`). Empty-for-these-ships is
not never-sent: the live `VOYAGE` record carries `cunning|storm-cutter|
song-keepers|quiet hull`, so the mud does populate them.

Two of the seven changed status:

**`FARM` — half resolved, still held.** `farm_plots[].name` *is* what MIP
calls `shroom_id`: MIP's own value is a display name
(`"Ergclaw Flyspot Ghostveil"`), settled by data rather than inference.
What blocks it is `weather_mod` from the `meta|` header, which the Farm
tab renders as "Weather modifier". `Guild.City.weather` is a different
quantity (season/weather/strength) and must not be substituted for it.

**`BLOT` — do not move it.** Both fields are present
(`{filled, reset_in, state, total}`) but `filled == total == 9` in all 21
packets, so `filled|total` versus `total|filled` is unresolvable, and the
Settlement tab prints the **raw string** — a wrong order ships visibly
wrong text. A mapping ambiguity rather than a missing field, but the
rule's purpose applies identically. It is one line of low-value text.

### LONGSHIP: the one lossy synthesis, and why it is still right

**Approved by the user, 2026-09-03.** `Guild.Voyage.longship` omits
`crew_traits` and `ship_traits`, so synthesizing `LONGSHIP` from it drops
two fields THE RULE would normally hold the key back for. Two facts make
it the right trade anyway.

**Nothing reads them.** The Sea tab's Fleet block renders name, tier,
state, target, return time, crew, and the saga pair — its own comment
says the voyaging ship's traits are spelled out in the Voyage block
above, which `VOYAGE` feeds. `VOYAGE` therefore **stays on MIP**: it
needs the same two fields and its renderer genuinely shows them.

**What it replaces was not a working feed.** MIP chunks `LONGSHIP` across
pushes the mud runs CONCURRENTLY, and `reassemble_chunks` matches a piece
to "the earliest push of that chunk count still missing this index" — a
coin flip between two live pushes. Measured on
`logs/mip_20260903_143404.log`: **320 of 364 completed pushes carried a
malformed record**, e.g. `2|Isoxl|5|voyaging|Deep Soad-hulled|merciless`
and `7|Wave Crow|4|raiding|Waterford|rford|2929`. GMCP paginates — five
pages of ships in ascending id order plus a terminator carrying the
`voyage` object — so there are no seams to mis-join.

Driving the captured sweeps through the real handler: **all 8 ships in
100% of publishes (1216/1216) with 0 malformed records**, against MIP's
88% corruption rate.

One trap in the data: a ship with no style sends the **integer `0`** for
`voyage_identity` and `captain_style`, where MIP sends an empty field for
those same ships. `str(0)` would have written a literal `"0"` where a
style name belongs. Confirmed by cross-reference against the MIP record,
not assumed.

This also retires the `merge_longship` cycle heuristic for GMCP sessions.
That function stays for MIP-only ones, where it was fixed the same day —
see the commit; the mud's rotating push order had broken its
ascending-id segmentation and made the Fleet block cycle 2→4→2 ships
every round.

### Not ported on purpose

`Guild.Market.market_0` is an exact shape match for the `MARKET` key
(`id|buyer|good|remain|price|age`, 5 byte-exact matches against MIP), and
it is **not** ported: `MARKET` has no `parse_*` and no renderer anywhere
in the repo. Porting a key nothing reads is cost without benefit.

### The package surface is much larger than either list

`Core.Supported` declared 18 packages and named none of the eight
`Guild.*` the Viking feed actually rides on. This capture found **three
more** undeclared and undocumented: `Guild.TradeGoods`, `Guild.Warehouse`,
`Guild.Info` (the last is just `{guild, age}`). `Guild.Extra` and
`Guild.Info` were declared *subscribed* at login while six of the eight
real carriers were not mentioned at all. Enumerate the wire; never the
declaration.

Shapes seen but not yet consumed, each a candidate for a later increment:
`Guild.Settlement.sproj`, `Guild.Trade.cidle/crpr/cupg`, `Guild.City
.builds/dcycle/nexttick/heat`, `Guild.Roster.bonds`, and `Guild.State`'s
`daler/chain/points/gxp/target/bars/gline1/gline2`.

`Guild.Settlement.settlerx` deserves its own change, not a ride-along: it
sends **24 named fields** where `SETTLERX` is 18 positional ones that have
been only partially decoded since June, with `employed` vs
`staffed_market_jobs` explicitly flagged unconfirmed. That is a *decode*,
and it should be verified against `vsettler` readouts the way the original
mapping was.

**What "live-tested" covers, precisely.** The 2026-09-03 session confirms
the *hand-over*: every key retired its MIP twin, on real data, with no
warnings. It does **not** confirm *render parity* — nobody has yet checked
the Viking tabs draw the same from the synthesized strings as they did
from MIP. That is still open for all eleven moved keys, and it is the
thing to watch on the next run. The second increment (TGOODS, WSTOCK) has
not run in a session at all; it is unit-tested against verbatim packets
and byte-verified against the MIP of the same session.

### The live test to run

Both transports in the SAME session — split across two, the comparison is
worthless.

1. `Toggle gmcp` on, then **reconnect** — telnet options negotiate once, at
   connect, so it takes effect only on the next connection. Same for the
   rollback: `Toggle gmcp` off, reconnect.
2. Confirm `[MIP handshake sent, pin NNNNN]` is in the scrollback. Absent →
   the takeover-login bug, run `#mip`. Do this **first**: a silent MIP has
   produced a false "GMCP broke MIP" conclusion twice, and this run has both
   transports live, which is exactly when that misreads.
3. `#raw` on, `#mipraw` on, `#log` on.
4. Watch for `[GMCP now owns viking ...]`. The keys expected to hand over
   are SHIPS, ROUTES, LMARKET, PATROL, WEATHER, PRODUCTION, SACTIONS,
   THRALL_FOLLOWER, STFX. Compare each tab against the MIP feed still
   running beside it.
5. To resolve the held-back keys, open the readouts that emit them so both
   streams carry the same value at the same moment: LONGSHIP and VOYAGE
   (voyage/fleet readout), CARTS (trade carts), FARM, BQUEUE (build queue),
   BLOT, THRALLS. Those seven are the whole remaining agenda.

One gap the 0903 captures cannot close: the synthesized `[50960]` viking
biome prefix in `gmcp_room_info` is still **unverified** — those logs contain
no `Room.*` at all. It needs a Room.Info captured while standing in the
biome.

## Tests

- `tests/test_gmcp_transport.py` — telnet negotiation both directions,
  default-off refusal, `IAC IAC` un-escaping, split packets, non-GMCP
  subnegotiations ignored.
- `tests/test_gmcp_feeds.py` — dispatch, supersession hand-off (including
  that superseded tags still reach `#mipraw`, and that `Char.Vitals` never
  retires `FFF`), the three ported handlers, and the `#roomrefresh`
  probe — payload shaping, and that a solicited `Room.Info` is never
  charted while an unsolicited one still is.
- `tests/test_viking_gmcp.py` — the Viking feed over GMCP, driven by
  verbatim captured packets: paginated sweeps, lineage-scoped
  replacement, the whole-value feeds, the held-back keys, the
  chunked-key (`SHIPS_1of3`) supersession trap, the `TGOODS` sweep
  (terminator page, numeric hold order, per-hold replacement), the
  `WSTOCK` sweep (batch identity, optional grade, wire order), and
  `PerConnectionResetTest`, which reads `connect()`'s source to guard
  that all four per-connection GMCP attributes are cleared there.

## 2026-09-15: MIP is gone, and the Viking window filled up again

The mud disabled MIP. `logs/mip_20260915_132858.log` carries **zero BBE
packets** against 25125 in `logs/mip_20260903_135126.log` of the same
feeds, and the guild channel confirms it ("had to be disabled for now",
then "eventually mip will definitely go away"). The user has since
confirmed it is not coming back. The wire is the proof; the chat is
corroboration.

That check is only meaningful because `handle_mip` writes to the
`#mipraw` buffer and the session log at the TOP of the function, before
the supersession test and before `mip_viking`'s per-key filtering. Zero
BBE in the log means zero BBE on the wire, not zero surviving our own
filter.

### Why it was empty for everyone, not just some tabs

`gmcp_on` read `self.setting("gmcp", False)`. GMCP was opt-in while both
transports ran, which was right then and wrong the moment MIP stopped: a
player who never ran `Toggle gmcp` got *nothing*. Now defaults to `True`.
`set_setting("gmcp", ...)` is written only from `_toggle_gmcp`, so a
character who never touched the toggle has no stored value and picks the
new default up; only someone who explicitly turned it off stays off.

### THE RULE is relaxed

"Synthesize only when every field the parser reads is present" existed to
stop a partial record retiring a working BBE feed. There is no feed to
retire any more, so holding a key back just leaves a tab blank. Now:
synthesize what GMCP sends and leave absent fields empty.

Per-KEY supersession STAYS - inert with no BBE arriving, and the only
cheap way back if it ever returns. And a POSITIONAL record missing a
MIDDLE field is still dropped whole (`_req`), because every later field
would shift one slot and the parser would read garbage rather than
nothing. `_rec` (fill with empty) is used only where the gap keeps its
slot.

### The login burst is the whole surface

Packets carry `"full": 1` at login and not after - `Guild.City`: **0 of
40** mid-session packets, **8 of 8** at login. Two mid-session captures
that day looked like most of the feed was missing; it was not. Every
mapping added here is pinned to `logs/gmcp_20260915_135025.log`.

Four packages had no handler at all. `Guild.TradeGoods` already had a
synthesis written, but `handle_gmcp` dispatches to
`gmcp_<package lowercased>` and `gmcp_guild_tradegoods` did not exist -
so TGOODS was dead code. `Guild.Warehouse`, `Guild.CityBuildings` and
`Guild.Map` are new.

### What the held-back keys were waiting for

| Key | was missing | GMCP sends |
|---|---|---|
| `LONGSHIP`, `VOYAGE` | `crew_traits`, `ship_traits` | `longship_crew_traits`, `longship_ship_traits`, and the `voyage_*` pair |
| `CARTS` | `legs` | `cart_legs` |
| `FARM` | `shroom_id`, `meta\|weather_mod` | `farm_plots[].name`, `farm_meta.wmod` |
| `BQUEUE` | the `used/cap` header | `bqueue_used`, `bqueue_max` |
| `BLOT` | an object, not a string | `blot` |
| `WSTOCK` | the real capacity header | `wstock_cap` = **8085**, the exact number the tier table got wrong |
| `THRALLS` | 20 named vs 16 positional | GAME FACT (user): thralls assigned per building, **all 20 valid**. `parse_thralls` reads the named form now |

### Three shapes that needed more than a field list

**Parent + child on different pages.** A raid and its plundered goods, a
cart and its legs, a refinery and its curing grades arrive as two flat
lists on DIFFERENT pages of one sweep (refinery on page 5, its grades on
page 3). Stitching them inside one packet silently drops every child.
They are buffered as two groups and joined once the sweep is whole, with
the child keyed `<parent id>#<page><index>`.

**Guild.Map is three separate sweeps** under one package, and they
disagree about which pages carry rows: `terrain` runs pages 1-7, `east`
and `south` run 2-8, and south's last page is short because MES has one
row fewer than MEE by design. Row index is therefore ARRIVAL ORDER within
the sweep, never the page number. `w`, `h` and `pos` never share a
packet, so VMAPH is assembled from `_viking_map_dims`, which a reconnect
reseeds from the VMAPH already in `viking_state`.

**CPEND is the outer grid side, not the interior.** `colony_grid` indexes
`CPT00..CPT<CPEND-1>` over 20 terrain rows while GMCP's `dim` is 12;
`margin` is 4 and so is `COLONY_INTERIOR_OFFSET`, so `CPEND = dim +
2*margin`.

### Verified

Replaying all 540 `Guild.*` packets of the login capture through the real
`_viking_gmcp` produces **197 state keys**, every consuming `parse_*`
returns data, and the real `VikingStatus` window renders **all 12 tabs**
with content - the Map canvas draws 2451 items, `warehouse_cap` reads
8085, `colony_grid` builds 20x20 with 47 buildings, `wall_rows_missing`
is 0. Tests: `tests/test_viking_gmcp_only.py`, plus the four
`test_viking_gmcp.py` cases that asserted the old hold-back rule, updated.

### Still dark

`VMAPL` - the named landmark/POI list - has **no GMCP source in any
capture**. The map draws and pathfinding has its graph (VMAPH + MEE/MES),
but `map_landmarks` returns nothing, so `Go <name>`/`VN` name resolution
and the Map tab's click-to-walk POI list have no targets. Worth an admin
question.

`STANDINGS`, `VREP`, `GRUDGES`, `SEVENTS`, `CELLAR`, `ENN`, `VCHH` and
`GOD_POWER*` did not appear in any capture either, but these are the
command-triggered class (the same shape as `Vskills`) - they should
arrive when the player runs the matching in-game command, and will show
up in live testing.

### `Go` by coordinate (2026-09-15, same day)

`Go <name>` resolves against `map_landmarks`, which reads **VMAPL** - the
one Viking feed with no GMCP source in any capture. Names are therefore
dead until the mud sends it, and the old failure message ("POIs still
loading - try again in a moment") was wrong in a way that wasted the
player's time: nothing was loading.

Nothing else about the walk needs VMAPL. `Guild.Map.pos` arrives on
**every step** (`logs/gmcp_20260915_211225.log`: 35,17 -> 35,16 -> 36,16
-> 36,17 -> 35,17, one packet each) and the MEE/MES grid is complete, so
`viking_walk_to` has everything it wants. `Go 35,17` (or `Go 35 17`) now
goes straight to it. A single number or a triple is still treated as a
name, so nothing that used to resolve stops resolving.

`#vmarks` falls back to the terrain grid when there are no names,
listing every non-ground cell by GLYPH and coordinate - 70 features on
the live map. Deliberately no invented names.

**What the glyphs mean is NOT pinned**, and the code says so. The one
datum: the lone `M` at (35,17) is the Midgard ENTRANCE - the player
walked there and the room carried an `enter` exit to 50957 that its three
neighbours did not. `L` occurs 13 times against the 13 non-capital
villages in `Guild.Trade.routes`, which is suggestive and nothing more.

**GAME FACT (user, 2026-09-15): "Midgard" names the biome AND the
guildhall, and the whole biome reports `Room.Info.area` "Midgard".** So
the area field identifies nothing inside the biome. An earlier note here
offered it as supporting evidence for the `M` cell and as a possible way
to learn POI names by walking - both wrong, and the idea is dead: the
`enter` exit is the only evidence, and no walk will ever yield a second
name. Getting out of the biome means reaching that entrance cell, which
is exactly what `Go 35,17` is for.

Incidentally this confirmed the VMR synthesis outright: a full grid
diffed against the mud's own `vmap` agrees **cell for cell**, 35x70, with
only a glyph substitution between them (`A`/`^`, `+`/`.`, `.`/`c`,
`W`/`~`) and exactly one leftover - the `X` the mud overlays at the
player's own position.

Tests: `GoByCoordinateTest`, `GlyphPoiFallbackTest`.

### `Go <name>` recovered from the old MIP logs

The user's point: the coordinates never changed, and MIP used to send
them. Both halves check out, and the logs say exactly how far to trust it.

Every VMAPL capture from **2026-07-17 onward** agrees with every other
AND with tonight's grid - **0 mismatches** across 61 POIs. The
**2026-06-14** capture disagrees on 16 of 46, every one of them by the
same `(+3, +1)`: a grid RE-ANCHOR, not settlements moving. (The
`map_landmarks` docstring used to say "settlements move, so this is
always read fresh" - that re-anchor is what it was really describing.)

Unioning the post-re-anchor captures recovers **21 world POIs**, and the
cross-check against the live VMR grid is exact: all 21 land on their
type's glyph, and **zero M/L cells are left unnamed**. That also pins the
legend outright - `M` capital, `L` lineage, `P` player village, `*`
mentor, `S` seer, `T` blot, `F` farm, `R` ruins - which accounts for all
70 features (21 world + 49 player villages).

So `WORLD_POIS` stands in when VMAPL is absent, **but only for entries
whose cell still carries the right glyph in the live grid**. That check
is the whole safety of it: the map was re-anchored once already, and a
frozen table across the next one would walk the player confidently to the
wrong cell. Verifying against the grid means a re-anchor silently empties
the table and `Go` says it has no names, rather than lying.

**Player villages are included.** GAME FACT (user, 2026-09-15): a player
CANNOT move their settlement - and the grid agrees, 48 of the 49 in the
old VMAPL still sitting on a `P`. The 49th, Callheim (3,21), reads plain
ground: it was razed, and the glyph check drops it rather than the table
having to know. A village founded since the last capture simply does not
resolve (one such cell on the 2026-09-15 grid).

**Names come from the LONGEST form seen at each cell, never the most
frequent.** A VMAPL chunk that starts mid-record loses the head of the
name, and that truncation can be the majority form: `VMAPL_3of4^^ormhofn
|29|12|normal` appears 651x against `player|stormhofn|29|12|normal` 147x.
There is no ormhofn (user confirmed) - a cut name is always a SUFFIX of
the real one, so longest wins. `player|ein|30|6|walker` and
`player|riya|62|21|camma` arrive as complete records, so those short
names are genuine.

69 POIs resolve by name again - all 14 `Guild.Trade.routes` villages, the
mentors and landmarks, and 48 player settlements. `VN` works, and
`Go 35,17` still handles anything unnamed.
Tests: `WorldPoiFallbackTest`.


### `settlements` is authoritative, and `Vsettlements` reads it

The mud has a `settlements` command that prints every settlement with its
coordinate. It is the source the VMAPL reconstruction was standing in
for, and it validated that reconstruction outright: **62 shared entries,
zero coordinate disagreements.**

It added two things the logs could not:

* **Kattegat (39,14)**, founded after the last VMAPL capture - the single
  unnamed `P` the grid had been showing all along.
* The real spelling of the names a chunk boundary had clipped
  (`Stormhofn`, `Ein`, `Riya`, `Nethells`, `Darkcity`, `Jarn-hesturheim`).

It also closed out Callheim. GAME FACT (user): it never existed - Jarl
Call's settlement is Kattegat, and Callheim was a transient from a spell
when Skuggis was debugging the biome and it had to be re-made. The glyph
check had already dropped it, which is the safety property working on a
real case before anyone asked it to.

`Vsettlements` sends `settlements` and merges the reply, the same
on-demand read `Vskills` does for skill costs. The readout is
authoritative for the three SETTLEMENT types, so those are replaced
wholesale - which is what retires a razed one - while the POIs it does
not list (mentors, seer, blot grove, farm, ruins) keep their
VMAPL-derived entries. A garbled capture merges nothing rather than
half-replacing the table.

So the table is no longer frozen: a settlement founded next month needs
one command, not an edit. 70 POIs resolve by name today.
Tests: `SettlementsReadoutTest`.


## 2026-09-20: the room path had not caught up, and the mapper was dead

`logs/gmcp_20260920_190531.log` / `logs/din-20260920.log` (Din,
bladesinger). The mapper tracked nothing on 3s: no `@` movement, no
charting.

**It was not a mapper bug.** The 2026-09-15 MIP removal was applied to the
Viking feeds but never to the room path, and the room path was the one
thing still leaning on a MIP packet that stopped coming.

### The proof

`logs/din-20260920.log` line census over 27 real moves: **96 GMCP, 134
LOCAL, 0 MIP**. That census is only meaningful because `handle_mip` tees
to the log at the TOP of the function, *before* the supersession test —
same property that proved the BBE removal. Zero `^MIP ` means zero on the
wire, not zero surviving our own filter.

### Why zero DDD killed both modes

`sql_on_ddd` is reachable **only** from `mip_exits`, which is reachable
**only** from the MIP `DDD` tag. So with MIP gone:

* `sql_follow_ddd` is the **only** setter of `_sql_cur_vnum`, the value
  `@` follows. `gmcp_room_info` synthesized the BAD half only, and
  `sql_follow_bad` refreshes the room NAME and nothing else — so
  **following froze**, showing the right name in the wrong place.
* `sql_reconcile_chart` early-returns while `_sql_bad is None or
  _sql_ddd is None`. `_sql_ddd` could never arrive, so **charting never
  charted a room**.

### The fix: synthesize both halves

`gmcp_room_info` now emits the DDD string as well
(`"~".join(dirs + [str(num)])`) and feeds it to `mip_exits`. The old
objection — doing both would run the follow/chart path twice per move —
died with the live DDD.

Three things the synthesis has to get right:

* **BAD before DDD.** `sql_reconcile_chart`'s first branch reads *a DDD
  with no BAD pending* as an in-place glance refresh and **releases the
  gate**. Emitting DDD first would trip that on every real move. 3s sent
  BAD first on the wire; the synthesis keeps that order.
* **Through `mip_exits`, not `sql_on_ddd`.** Same `map_backend` guard
  `mip_room` crosses.
* **After the `_room_refresh_info_due` guard**, so a solicited
  `#roomrefresh` reply still charts nothing.

An exitless room (dark rooms report none) still emits its DDD — the vnum
*is* the location, and dropping the packet for want of exits would freeze
`@` for exactly the rooms least able to afford it. `"280"`, not `"~280"`.

Tests: `RoomInfoDrivesLocationTest` asserts on `_sql_cur_vnum` through the
real `sql_*` chain rather than on a mock call, and fails `None != 11`
against the pre-fix code.

### STILL OPEN: the glance signal has no replacement

`Room.Info` fires on **movement only** — the mud still sends nothing for
`look`/`glance`. The bots' in-place "the room displayed" signal
(`_chaossea_on_room_packet`, reached via `sql_follow_ddd`) is therefore
restored *on movement* and still absent *in place*. `Room.Refresh` is
documented but dead on 3s, so there is no structured replacement yet.
Post-kill re-scan is the case to check first.


## 2026-09-20 (2): the enemy bar, and a full FFF casualty audit

`logs/gmcp_20260920_192707.log` (Din, bladesinger, mid-fight). The enemy
status bar never populated.

Same root cause as the mapper: `vitals["enemy"]` and `vitals["enemycond"]`
came from FFF's `K` and `L` fields (`protocol.py` `COMPOSITE_TEXT` /
`COMPOSITE_NUMERIC`) and **nothing else writes them**. FFF died with MIP.

`Char.Combat` carries both — `attacker` and `attacker_hp`, which
`gmcp.txt` states is a health **percentage**, exactly what
`update_vitals` wants (`bar.set(enemycond, 100)`). The handler existed but
only consumed the *empty* end-of-fight snapshot.

### What the fix had to carry over, not just the two fields

* **The empty snapshot must now CLEAR the bar.** Once this handler
  populates it, it is the only thing that empties it.
* **The enemy-change round reset** (`client.py:9430` on the FFF side).
  Back-to-back aggro sends no clear packet at all, so the old fight's
  count survives and `katmud_scripts` rule 1 (`rounds >= 5` + a fresh
  enemy) fires a psummon on round ONE. Four psummon leaks trace to this.
  The guard has to live on whichever transport supplies `enemy`.
* **`rounds` is NOT taken from the packet.** `katmud_scripts.py:252-262`
  is explicit that assigning it wholesale from the mud is why `efdd1b0`,
  `e556a16` and `8a918b3` each failed to stop the same misfire. With FFF
  gone `_fff_combat` is permanently False, so the damage-line branch
  (`client.py:12353`) is a genuine client-side count. Left alone.
* **`in_combat` still untouched**, for the reason already documented.
* **`target` gated to `"you"`.** `gmcp.txt`: target is the literal `"you"`
  when it is you, "otherwise that victim's name". Every frame in the
  capture is `"you"`, so the other case is UNOBSERVED — gate rather than
  paint another player's fight onto this bar.

Accepted narrowings, both stated in the docstring: a **passive mob** that
never fights back produces no packet and no bar; and **two mobs on you**
alternate in `attacker`, re-zeroing the count each flip. Both err the way
this handler already prefers — a psummon delayed, never wrongly fired.

Tests: `CharCombatEnemyBarTest`. Two pre-existing `CharCombatTest` cases
were updated, not deleted: they asserted a live packet left the counter
alone, which was right only while FFF also ran and did the reset.

### FFF casualty audit — every field, and whether anything feeds it now

| FFF field | source today | status |
|---|---|---|
| `hp` `sp` | `Char.Vitals` | OK |
| `hpmax` `spmax` | `Char.Vitals` `maxhp`/`maxsp` -> `vitals_max` | OK |
| `enemy` `enemycond` | `Char.Combat` | **FIXED HERE** |
| `round` | damage-line trigger, client-side | OK, and preferred |
| `gp1/max` `gp2/max` | `Guild.State` via `GMCP_GUILD_POOLS` | **BLADESINGER ONLY** — every other guild's pool bars have no source |
| `hpbar1` `hpbar2` | nothing | **DEAD** — bard song timer + 8-buff maintenance, changeling bioplasts, necro reset%/Status, gentech status bars |

### Second casualty: per-login setup has not run since MIP died

`mip_vitals_composite` opens with the whole login block — `confirm_login`,
`setup_commands` (the 3s room-marker `aset`s and `DISPLAY_ROOMID`),
`_powers_request`, `login_commands`, `_send_on_activate`. All of it is
gated on an FFF packet arriving. **Not ported, not fixed here** — flagged
because it silently disables room markers, which is the mapper's own
input.


## 2026-09-20 (4): in_combat lost its authority, and every bot hung on it

Reported as "`Run recruits` kicked off but didn't move." Confirmed live:

    [cstats] in_combat=True fff_combat=False enemy=''
             run_on=False run_killing=True run_engaged=True

`in_combat=True` with `enemy=''` is the signature: it believes it is
fighting nothing.

### Why

FFF's `K` field was "the sole authority for ending a fight"
(`client.py:9461`). With MIP gone on 3s, `in_combat` is set by the
damage-line branch and cleared **only** by a kill-text match — so a fight
that ends any other way (fled, outrun, wandered off, killed by someone
else) leaves it True permanently.

`_run_tick` then latches:

```
if self.in_combat and not self._run_killing:   # _run_killing = True
if self._run_killing:
    if self.in_combat:
        self._run_reschedule(1.0); return      # forever, NO output
```

Every other branch that returns without stepping is bounded and
announces itself (`_run_waiting_room` 1.3s ceiling, `_post_kill_holding`
deadline + message, `_run_held` deadlines + message, `_run_room_blocked`
prints). This one is the only silent unbounded loop — which is why it
presented as "nothing happened at all". `Bot`, `Hunt` and `Chaossea` gate
on the same flag.

### Fix: a damage-line staleness release

`_combat_stale_check`, called once a second from `_tick_body` ahead of
anything that gates on `in_combat`. Every landed blow stamps
`_last_damage_t`; if `in_combat` stands longer than `combat_stale_s`
(default **8s**, ~4 roundless rounds at 3s's 2s round) the fight is
assumed over and the flag is released.

* **Gated on `not _fff_combat`** — the same gate the damage-line branch
  uses. 3k still runs MIP, where FFF ends fights correctly, and no
  timeout may override it. (GAME FACT, user 2026-09-20: **3k has no GMCP
  at all**; only 3s switched.)
* **A damage line, not Char.Combat's empty snapshot.** Char.Combat only
  reports what is attacking YOU, so a passive mob you are killing may
  never appear in it and its end marker could fire while that fight is
  still going. Whiffing a whole round sends no damage line either, which
  is why this is a timeout rather than a single missed round.
* **It announces.** A bot walking off a fight with no explanation is what
  made this take a session to find.

Tests: `tests/test_combat_stale.py`, including one that drives
`_tick_body` to guard the WIRING (the method existed unwired for a
commit) and one pinning `DAMAGE_RE` to the real line.

### Related, still open

`pwho` is rejected in some rooms ("There is no reason to 'pwho' here"), so
the party whitelist seeds empty there. And the bots' mob/player detection
reads the `look_monster italics` / `look_player underline` markers, which
are `setup_commands` — part of the dead login block above. On 3s right now
the bots likely cannot see mobs or players by marker at all.


## 2026-09-20 (5): Room.Death captured; the stale release LIVE-CONFIRMED

`logs/gmcp_20260920_200313.log` (Din, bladesinger, giant training
grounds). `Room.Death` is a NEW package, added mud-side last boot (user).

**The stale release worked live.** `[combat] no blow landed for 8s -
fight assumed over` fired once, correctly, and the bot resumed. The long
sit the user reported was that 8s on top of the bladesinger revalrie
post-kill hold (`_arm_post_kill_resume`), which is intentional.

### What the capture settles about Room.Death

```
Char.Combat { "target":"you","rounds":141,
              "attacker":"A giant elite recruit in training","attacker_hp":1 }
Room.Death  { "corpse":1,"killer":"Din","npc":1,
              "name":"A giant elite recruit in training" }
Char.Combat { "target":"","rounds":0,"attacker":"","attacker_hp":0 }
```

* It **fires for your own kills** (`killer: "Din"`), not just a merc's.
* Ordering, consistent across both captures: `Room.Death` arrives
  **before** the empty `Char.Combat`, and **both arrive before the next
  damage line**.
* `name` is the **long formal name** and does NOT match the kill text's
  short name ("A giant elite recruit in training" vs "Giant Recruit").
  Do **not** match these two by name. (The earlier Giant Ant pair differed
  only in case, which understated this.)
* NOT yet observed: an uncredited kill (`killer: ""`) or a player death
  (`npc: 0`).

### Room.Death cannot end a fight

Tempting, because it is an explicit kill event. But it arrives BEFORE the
trailing damage line, so clearing `in_combat` on it is undone one line
later - exactly as the kill text already is. It is worth wiring for kill
counting, the recent-kills pane and `corpse: 1` (which the corpse cascade
currently has to guess), and for nothing else.

### OPEN: is the trailing damage line post-mortem, or a second mob?

`You hit Giant Recruit 4 times for 5055 damage.` lands AFTER
`Din dealt the killing blow to Giant Recruit.` Two readings, and they
demand opposite fixes:

* **Same mob** - the tail of the killing round hitting a corpse. A short
  post-death suppression window would remove the 8s wait entirely.
* **Different mob** - `Char.Combat` and `Room.Death` both name the elite
  recruit while the text names "Giant Recruit", the room holds 11
  monsters, and `Char.Combat` is blind to mobs that do not attack you. Then
  the damage line is a REAL ongoing fight and suppressing it would walk
  the bot off a live mob.

The name evidence cuts both ways (short name vs long name of one mob, or
two genuinely different mobs). **Do not build the suppression until this
is answered** - ask the user, per [[feedback-no-game-mechanic-assumptions]].


### CONFIRMED: the phantom fight costs a redundant 40s hold

`[bot: resume condition never came - resuming]` was observed, proving the
chain:

1. kill text -> fight ends -> kill-trigger runs `mw`/`revalrie`, and the
   post-kill hold releases correctly on *"Fully refreshed you end your
   revalrie."*
2. the trailing damage line re-arms `in_combat` -> **phantom fight**
3. `_combat_stale_check` clears it 8s later -> the Run bot reads that as
   another fight ending -> `_arm_post_kill_resume()` fires AGAIN (the lone
   `look` is the tell; `recruits.json` has `after_kill: []`)
4. that second hold waits for revalrie text that already arrived in (1),
   so it burns the full `post_kill_resume_timeout` - **40s** for a
   bladesinger

`combat_stale_s` is NOT the knob for this; the 40s dominates.

CORRECTION to the note above: `_post_kill_holding` is NOT always bounded.
`post_kill_resume_timeout: 0` sets `_resume_deadline = None` and holds
**forever, silently** - deliberate (`client.py:4988`), because a full sp
refill is ~23 minutes and a 40s net was cutting real revalries short.
Bladesinger is 40, so the "every other branch is bounded" claim held for
Din but is not a property of the code.

### NOT the cause: the ritual scene (checked, negative)

`_run_match_mob_line` returns the WHOLE matched line as the display name,
so a ritual-scene match would have printed
`A ritual scene depicting a giant elite recruit in training, spread by
the Blood Eagle -> kill recruit`. The observed line was
`[run] A giant elite recruit in training -> kill recruit`, i.e. a real
mob line.

**LATENT GAP though:** the ritual-scene filter exists ONLY in
`_chaossea_scan_markers` (client.py:5876). `Run`, `Bot` and `Hunt` have
none - and a bladesinger leaves *"A ritual scene depicting a <mob>, spread
by the Blood Eagle."* in every room it kills in. On a LOOPING route that
scene is still there next lap, contains the mob keyword, and would match
as a target - costing a whiffed `kill` plus the engage grace each lap.
Not yet observed; worth watching on a second lap.

### FIX: post-mortem damage suppression (root cause of all of it)

GAME FACT (user, 2026-09-20): the training grounds hold exactly **two**
mobs, *A giant elite recruit in training* and *A giant elite guard in
training*. The combat text renders them "Giant Recruit"/"Giant Guard" -
so the trailing damage line IS the killing round's tail landing on a
corpse, not a second mob.

`_is_post_mortem(target)` - a damage line naming the mob we just killed,
within `postmortem_s` (default 5s), does not re-arm `in_combat` or tick
`rounds`. The `#dmg` tallies still count it; the damage was real.

* **Matched BY NAME, not by a blanket timer** - killing the recruit
  cannot silence damage against the guard beside it. That is the test
  worth keeping (`test_a_second_mob_still_starts_a_fight`).
* The kill text and the damage line share the SHORT name form while
  `Room.Death` uses the long one, so **Room.Death cannot substitute
  here** - the one place its name field would have been the obvious
  source and is the wrong one.
* Time-bound, never count-bound: one round can land several trailing
  lines, and the window stops a same-named respawn being swallowed later.
* 3s-only by construction: every caller sits inside
  `if not self._fff_combat`, and 3k still runs FFF.

`_combat_stale_check` STAYS. This fix covers fights that end in a death;
the timeout still covers fights that end with no death at all - fled,
outrun, killed by someone else. Two mechanisms, two cases.

**FRAGILITY:** this hangs off the kill-text pattern. Gag it, or let the
mud reword it, and suppression silently stops working and the 40s hold
returns.

Tests: `PostMortemDamageTest` (predicate) + `PostMortemEndToEndTest`
(drives `track_combat_line` with the two real lines in order, so the
WIRING is covered too). 1353 green.


## 2026-09-20 (6): psummon stopped firing - the on_vitals HOOK

`call_hook("on_vitals", ...)` fired from exactly ONE place: the FFF
handler `mip_vitals_composite` (`client.py:9570`).
`katmud_scripts.on_vitals` is where **both psummon rules** and the
elemental **evoke-age** live, so with MIP gone none of them ran at all.

Same class as `in_combat` and `confirm_login`: not a FIELD FFF wrote, but
something reachable **only** from FFF. The field audit could not have
found any of the three - worth remembering the next time a feed "looks
covered".

**FIX:** fire it from `gmcp_char_vitals`, and nowhere else on the GMCP
side. Char.Vitals is the closest analogue to FFF's per-round vitals
packet and arrives reliably every round; firing from Char.Combat as well
would run the rules twice a round.

Char.Vitals arrives BEFORE Char.Combat in every capture, so `enemycond`
is one packet stale at hook time. Harmless (rule 1 needs `ticks >= 5`),
but it is why the two are not interchangeable.

**The transport under these rules changed completely and the rules did
not have to.** `client.rounds` now comes from the damage-line branch
client-side instead of FFF's N, and `enemycond` from Char.Combat's
`attacker_hp` - but `_ticks` counts OBSERVED MOVEMENTS of the counter, so
it never cared where the number came from. Pinned by
`GmcpEraTicksTest`, including that rule 1 actually sends `psummon` again.

Note five packets give FOUR ticks: the first packet of a fight is a first
SIGHT, not a movement, and is deliberately uncounted - that is the whole
defence against a sprint's destination packet reading as fresh.


## 2026-09-20 (7): Room.Contents settles the ritual scene

Live: `[run] A ritual scene depicting a giant elite recruit in training,
spread by the Blood Eagle -> kill recruit`, then three `There is no
recruit here.`

The scene renders in the **mob-line slot** and is italic, so no marker can
tell it from a mob (hence `_chaossea_scan_markers`' explicit text filter).
The asets that set italics are `setup_commands` and currently dead anyway.

**GMCP just says so.** `logs/gmcp_20260920_203053.log`, a room the user
confirmed holds 5 recruits, 2 guards and 2 scenes:

```
{"type":"monster","count":5,"name":"A giant elite recruit in training"}
{"type":"monster","count":2,"name":"A giant elite guard in training"}
{"type":"item",   "count":1,"name":"A ritual scene depicting a giant elite recruit in training, spread by the Blood Eagle"}
{"type":"item",   "count":1,"name":"A ritual scene depicting a giant elite guard in training, spread by the Blood Eagle"}
```

New `gmcp_room_contents` stores both sets; `gmcp_room_info` clears them
(the two are strictly paired, 9/9, Info always first).

**A BLACKLIST, not a monster whitelist.** `_run_match_mob_line` rejects a
line whose normalised name is a known ITEM. Room.Contents is **entry-only**
(never once arrives alone in the capture), so the sets go stale as mobs
die - but *scenery does not change while you stand in the room*, so the
blacklist stays correct for the whole visit. A whitelist would go stale the
other way and hide a mob that wandered in after entry.

Empty set = behaviour unchanged, which is the regression guard for **3k,
which has no GMCP at all**.

Counts are stored but read by nothing yet - free here, and the obvious
next use.

Not touched: `Bot`/`Hunt` match through `_bot_scan_markers`/italics, a
different path. The same blacklist would apply there.

Tests: `tests/test_room_contents.py`, driven with the room's REAL printed
lines including the `{5}`/`{2}` count tags - including
`test_a_contains_whitelist_is_saved_by_the_blacklist`, i.e. the exact
`{"match":"recruit","contains":true}` config that broke.

### Room.Contents arrives BEFORE the room text (user, 2026-09-20)

Confirmed in every capture: `Room.Info` -> `Room.Contents` -> `Room.Map`,
then the rendered room. So a bot need not wait for the text at all - the
monster list is already in hand when the step's reply starts arriving.
`_run_step_sent`/`_run_on_prompt` currently poll for the PROMPT with a
`run_move_ms` (1300ms) ceiling. NOT YET DONE; this is the obvious speedup.


## 2026-09-20 (8): the 1300ms per-step wait was never meant to exist

`_run_on_prompt`'s docstring says *"run_move_ms stops being the pace and
becomes only a ceiling."* **On 3s that is false.** `prompt` events require
telnet GA/EOR (`protocol.py:412`) and **3s sends no GA**, so
`_run_on_prompt` never fires, `_run_room_ready` never flips, and every
step burns the full ceiling. 27 steps x 1.3s = ~35s of dead time a lap,
and `_run_send_step` has no throttle of its own - the wait WAS the pace.

### Release on Room.Contents instead

The GMCP room packets arrive BEFORE the rendered room text (user-confirmed
and visible in every capture: Info -> Contents -> Map -> text), and
Contents carries the mob list outright. So the room has landed the moment
Contents arrives and there is nothing left to wait for. The new pace is
one network round trip, self-limiting because the bot cannot step until
the mud answers the last step.

Three things this needed to be safe:

* **A GMCP mob source.** Releasing early without one means scanning an
  empty `_run_lines` and walking past mobs - the exact failure the startup
  `glance` + `_run_step_sent()` was added to prevent ("kayos left 'A Noisy
  Battleground' unfought every run"). `_run_scan_mob` now consults
  `_gmcp_room_monsters` between the latch and the text sweep.
* **`gmcp_room_death` to keep the counts honest.** Room.Contents is
  entry-only, so counts go stale with the first kill the moment they are a
  mob SOURCE rather than an item blacklist. Room.Death carries the SAME
  long name form Contents uses - which is exactly the form the kill text
  does NOT use, the mismatch that stopped it being the post-mortem signal.
  Still does not end a fight, for the reason in (5).
* **`expect_room`.** Only a MOVEMENT step releases on a packet. The
  startup glance and the post-kill re-scan call `_run_step_sent()` too and
  no Contents will ever come for them, so they keep the timer. A failed
  move sends no room packet either and correctly falls back to it.

Names are keyed lowercase (Room.Death casing has differed - "Giant ant" vs
"Giant Ant") with the original spelling kept alongside, because the bot
prints it.

**KNOWN LOSS, deliberate:** `_run_room_attackers` reads `RUN_ATTACKING_RE`
off buffered TEXT to hold movement when a mob is already mid-attack and
`in_combat` has not caught up. On the fast path that buffer is empty, so
the guard is effectively off. Room.Contents cannot replace it - `gmcp.txt`
says monster health and target appear there only if your skills would show
them in the look text.

Scope: **Run only.** `Bot`/`Hunt`/`Chaossea` have the same GA problem via
their own gates, but each has different room-ready semantics and
Chaossea's glance-spam history makes it the worst place to experiment.
Verify Run live first, then port.

Tests: `NoGaNoWaitTest` (release on movement, timer kept for the post-kill
re-scan, and the mob found with ZERO text lines) + `RoomDeathDecrementsTest`.

**LIVE-CONFIRMED 2026-09-20** (user): *"Runs right to the first mob instead
of parking in every empty room for a second."*


## 2026-09-20 (9): psummon fires - but on a counter that never reset

`logs/gmcp_20260920_205413.log`. **The on_vitals fix works**: both rules
fired and the shadow portal opened twice.

```
[auto-psummon: 114 rounds fought, fresh enemy (98%)]
Char.Combat { "rounds": 2,  "attacker_hp": 98 }
[auto-psummon: long fight (131 rounds), enemy at 78%]
Char.Combat { "rounds": 22, "attacker_hp": 78 }
```

114 against a real round 2. `_ticks` reset **only on a change of enemy
NAME**, and the Angarboda training grounds hold nothing but
*"A giant elite recruit in training"* - so the name never changed between
mobs and the counter accumulated across every one the bot killed. Rule 1
("round 5+, enemy still fresh") therefore fired on round ~1 of every
fight, spending a limited `psummon_left` each time.

**FIX:** also reset when `client.rounds` **drops**. It is zeroed by the
kill-text branch, so a drop can only mean the last fight ended. Guarded
for `_rounds_seen is None` on a fresh session.

**NOT fixed by reading `Char.Combat.rounds`** - tempting, because it
resets 0->31 cleanly in this capture, but `katmud_scripts.py:206-213`
documents the trap: it is the ATTACKER's fight duration (an orc reported
1253). A mob already fighting someone else when you engage hands you an
inflated number, which is the original sin `_ticks` exists to avoid. The
capture only looks clean because that mob was fresh.

**Gap closed with it:** `_combat_stale_check` cleared `in_combat` but left
`self.rounds` high - a fight ending with NO death has no kill text, so
nothing zeroed it and no drop ever occurred. It now calls `_stash_rounds`
(not `= 0`, so the recent-kills pane keeps `_rounds_at_end`).

Tests: `SameNamedMobsTest` (two consecutive identically-named mobs: ticks
reset, rule 1 does NOT fire on round 1, and still fires once the new fight
earns it) + `StaleReleaseZeroesRoundsTest`.


## 2026-09-20 (10): "GEE, what are we fighting then?" x2 per mob

The Run bot sent `kill recruit` three times for one mob. Not a bug in the
re-poke - a bug in what it is waiting for.

`_run_tick`'s engage-grace branch re-issues the kill every
`run_settle_ms` (1500) until `in_combat` flips, inside
`run_engage_grace_ms` (4000). Ticks land at 1.5s and 3.0s, so **two**
re-pokes - and the mud answers each with *"GEE, what are we fighting
then?"*, its already-in-combat reply.

`in_combat` was waiting for **YOUR first damage line** - and `DAMAGE_RE`
matches the mud's AGGREGATED once-per-round
`"You hit X N times for M damage."`, so it cannot flip until a full round
has resolved. (Not a tanking artefact - user corrected that; it is just
the round cadence.)

**FIX: `Char.Combat` now ARMS `in_combat`.** It lands before that first
damage line, so the 3.0s re-poke goes away. The 1.5s one stays, and
should - it is the design's own guard against a kill that whiffed on a
keyword. Three sends -> two.

**LIVE-CONFIRMED 2026-09-20** (user): *"It didn't keep spamming kill
recruit this time."*

### The asymmetry is the point, not a loophole

`gmcp_char_combat` still **never clears** `in_combat`, for the reason it
always did: it reports who is attacking YOU, so a passive mob you are
killing may never appear in it and clearing on its empty snapshot could
end a live fight. **Setting** carries no such hazard - if something is
attacking you, you are in a fight. Half the original decision is reversed
and half stands; `CharCombatEnemyBarTest` now pins both halves separately
so neither gets "restored" by mistake.

Gated on `target == "you"` (another player's fight must not arm yours) and
sequenced AFTER the enemy-change reset, so a new fight zeroes the round
count and only then arms combat.

### Why not the "GEE" line

It is definitive, but it is 3s-specific text and it only arrives AFTER a
wasted send. Char.Combat carries the same fact earlier and structurally.


## 2026-09-20 (11): a damage line became the target

```
[run] You hit Giant Recruit 4 times for 2638 damage -> kill recruit
> kill recruit
There is no recruit here.        (x3)
```

`_run_lines` is **cleared at the kill**, so the post-mortem damage line
lands in the freshly-empty buffer - and `{"match":"recruit",
"contains":true}` matches "recruit" anywhere in any buffered line. It also
**latches** (`client.py:7041`), and `_run_scan_mob` prefers the latch over
everything, including the GMCP monster list.

A `DAMAGE_RE` filter is NOT the fix: *"Eris trounced Giant Recruit up and
down."* matches too, and a blacklist of combat verbs is guesswork about
mud text.

**FIX: once Room.Contents has been seen for this room, GMCP decides
WHETHER a name is a mob; the text only decides WHICH.**

This is **not** the stale monster whitelist rejected in (7). It never asks
whether that mob is still ALIVE - the counts go stale and are ignored
here - only whether the mud ever called that NAME a monster in this room.

* `_gmcp_room_seen` is tracked **separately from the dicts**: once
  `gmcp_room_death` drains the last mob, `_gmcp_room_mob_names` is empty,
  which is otherwise indistinguishable from "this mud sends no
  Room.Contents at all" - and an emptied room is exactly where this bug
  happened.
* The rejection lives **inside `_run_match_mob_line`**, so the latch path
  at 7041 crosses it too (`LatchIsGatedTooTest`).
* Never seen = unchanged behaviour = the 3k regression guard.

**The structural discriminator would be italics** - `look_monster
italics` - and it is unavailable because `setup_commands` has not run
since MIP died. **Third symptom now pointing at the login block**; this is
a workaround for a missing input, not a fix for it.

**Still worth the data change:** `contains: true` is documented as "the
looser keyword style the script bot used". Exact names in
`muds/3s/bots/recruits.json` kill the ritual scene, the damage line and
the ally lines outright, and add the guard the recruit-only entry skips.


## 2026-09-20 (12): per-login setup, the fourth casualty

The whole login block sat inside `mip_vitals_composite` - the FFF handler
- so it was gated on a packet that stopped coming on 2026-09-15.
`confirm_login` was reachable from **nowhere else at all** (one caller).
Since MIP died:

* a prompted password is never **stored** ("store on success"), so the
  prompt returns every login;
* the priming `hp` never sends, and nothing backfills it - GMCP only
  sends a package when its value CHANGES, and the blur/portal charge
  counts `n/m` are text-only in the hpbar with no packet equivalent;
* **`setup_commands` never runs** - no room-marker asets, no
  `DISPLAY_ROOMID`. That is the mapper's and the bots' own input, and
  `look_monster italics` is the structural way to tell a mob from a Blood
  Eagle ritual scene. Its absence is why (7) and (11) had to be worked
  around through Room.Contents instead of just reading the marker;
* `login_commands` (guild init) never runs.

**FIX:** extracted to `_login_setup()`, called from **both** vitals
handlers - FFF for 3k (still MIP-only, no GMCP at all) and `Char.Vitals`
for 3s. Idempotent by `_logged_in` / `guild_login_sent`, so whichever
arrives first wins and the other is a no-op, which is also what makes it
safe on a mud running both transports.

`Char.Vitals` is the right GMCP trigger: it is the direct analogue of the
FFF packet it replaces, and it cannot exist before there is a character
in the world.

### The shape, now with four instances

| casualty | kind | found by |
|---|---|---|
| `enemy`/`enemycond` | fields | the FFF field audit |
| `in_combat` | a FLAG FFF cleared | a silent bot hang |
| `on_vitals` | a HOOK FFF fired | psummon never firing |
| `confirm_login` + setup | a CALL only FFF made | this |

Only the first was a field, which is why the field-by-field audit found
one of four. **When a transport dies, grep for every flag, call and hook
reachable only from it - not just the fields it wrote.**

### How to verify live

The block announces itself; no capture needed. On the next login:

```
[3s setup: N command(s)]
[guild init: N command(s)]
[password stored in credential manager]     (only if one was typed)
```

If those print but mobs still are not italic, the trigger fires before
the mud will accept commands and the block should hang off `Room.Info`
instead - THEN a `tools/gmcp_probe.py` capture earns its keep, because it
logs from the first byte with no race.

Tests: `tests/test_login_setup.py`, including that the FFF path is
unchanged for 3k.

**LIVE-CONFIRMED 2026-09-20** on a LINKDEAD reconnect. `[3s setup: 3
command(s)]` printed and the mud answered every one:

```
look_monster_pref set to: <ESC>[3m
look_player_pref  set to: <ESC>[4m
Property: DISPLAY_ROOMID   Value: 1
```

...followed by the hpbar, i.e. `confirm_login`'s priming `hp`. So
Char.Vitals arrives EARLY ENOUGH that the mud accepts the commands - the
Room.Info fallback is not needed.

Two harmless things visible in the same capture, neither worth changing:
`[MIP handshake sent, pin 19011]` still fires (the trigger is the generic
"elcome", here from *"3Scapes welcomes you back from linkdeath"*, and 3k
still needs the handshake), and no `[guild init:]` line because
`login_commands` is empty for this guild.

**The italics marker is live again**, which means (7) and (11) are now
working around an input that exists. Both stay as they are: the GMCP
classification is structured and does not depend on an aset the mud could
drop again.
