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
| `BAD` room, `DDD` exits | `Room.Info` | stage 1 — synthesizes both strings, feeds the existing path |
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

## Tests

- `tests/test_gmcp_transport.py` — telnet negotiation both directions,
  default-off refusal, `IAC IAC` un-escaping, split packets, non-GMCP
  subnegotiations ignored.
- `tests/test_gmcp_feeds.py` — dispatch, supersession hand-off (including
  that superseded tags still reach `#mipraw`, and that `Char.Vitals` never
  retires `FFF`), and the three ported handlers.
