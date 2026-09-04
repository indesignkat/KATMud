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
