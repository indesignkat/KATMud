"""3s Bard parsing + buff maintenance.

The Bards guild has NO private MIP tag - everything rides the mud-wide FFF
composite feed, in the two text fields:

  I (hpbar1)  active effects, space separated, `TOKEN` or `TOKEN:rounds`:
                R:1078 AG:877 O:873 ab RF PR:318 PE:401 B:762 MB:1655 HS:280
  J (hpbar2)  guild status + the song currently being performed:
                S:4;40% Let It Be: 59  NGL: 7867838

WIRE FACTS (logs/3s bards.log, all confirmed by an `AAA bard_x` line landing
immediately before the token appears / jumps - not inferred from docs):

* The I payload is REPLACE, not merge. An effect that lapses simply VANISHES
  from the list rather than going to 0:
      I~ ab MB:76        <- no RF
      AAA bard_righteous_fury
      I~ ab RF MB:75     <- RF back
  So "token absent" means "not up", and callers must rebuild their whole
  picture from each I packet instead of updating fields in place.

* Numbers are ROUNDS REMAINING (user-confirmed). Every counter drops by
  exactly 1 per I packet, in lockstep, so one I packet == one round.

* The three SONGS count UP while being sung - performing banks duration at
  a per-song rate (O +140/round, AG +182, R +132) - then settle once on
  `AAA bard_ceased_song` and decay 1/round like everything else. A rising
  counter mid-song is correct, NOT a bug.

* `cast refresh` tops up all three songs at once and does nothing to the
  spells. Wire proof (line 983): R 1451->1500, AG 1222->1299, O 1200->1295
  while PR 773->740, PE 856->823, HS 735->702 kept right on decaying. Those
  ceilings are SONG_CAPS below.

* Spells are recast one at a time.

Three tokens carry no counter and NEVER wear off (user-confirmed), so they are
displayed but never maintained - there is nothing to threshold on:
`ab` anticipate blows, `AB` avoid blows, `RF` righteous fury. `ab` and `AB` are
mutually exclusive stances, and differ only by case - so a token must never be
casefolded.
"""
import re

# token -> display label. Only tokens whose identity the wire actually
# proves. Anything else is shown raw rather than guessed at.
EFFECTS = {
    "R": "I Am A Rock",
    "AG": "Amazing Grace",
    "O": "O Muse",
    "PR": "Protection",
    "PE": "Prot. from Elements",
    "HS": "Hardened Skin",
    "B": "Blink",
    "MB": "Mind Blank",
    "RF": "Righteous Fury",
    # Permanent, counterless, never maintained (see below). Case matters:
    # `ab` and `AB` are DIFFERENT effects, so never casefold a token.
    "ab": "Anticipate Blows",
    "AB": "Avoid Blows",
}

# The three songs `cast refresh` tops up, and the value each returns to.
# Used only to show "1478/1500" - never to decide when to act (that's the
# threshold), so a cap that drifts with guild level degrades the display
# without breaking maintenance.
SONG_CAPS = {"R": 1500, "AG": 1299, "O": 1295}

SONGS = ("R", "AG", "O")

_STATUS = re.compile(r"S:(\d+);(\d+)%\s*(.*?)\s{2,}NGL:\s*(\d+)")
_SONG = re.compile(r"^(.+?):\s*(\d+)$")


def parse_effects(stripped):
    """I-field text (AFTER strip_mip_colors) -> {token: rounds or None}.

    None means the token carries no counter (`ab`, `RF`). Tokens absent from
    the text are absent from the dict - see the REPLACE note above.
    """
    out = {}
    for tok in stripped.split():
        name, _, num = tok.partition(":")
        if not name:
            continue
        try:
            out[name] = int(num) if num else None
        except ValueError:
            out[name] = None
    return out


def parse_status(stripped):
    """J-field text -> {smiles, reset_pct, ngl, song, song_left}.

    `S:4;40%` is Smile (user-confirmed): 4 smiles banked, 40% of the way to
    the next one. A smile is a reserve of extra sp that tops your sp back up
    until it runs out - so `smiles` is a resource worth watching, not a level.
    The percent counts 0->99 and WRAPS, which is why it must not be read as
    progress toward a guild level despite sharing the line with NGL. The
    count held at 4 across all 761 J fields in both captures (none were
    spent), so nothing observed says whether 4 is a cap.

    `song`/`song_left` are omitted when nothing is being performed - the
    segment is simply empty then, leaving the double space that separates
    it from NGL (`S:4;39%   NGL: ...`). J is the authority on what is
    playing; `AAA bard_ceased_song` is an event, not the state.
    """
    m = _STATUS.search(stripped)
    if not m:
        return {}
    out = {"smiles": int(m.group(1)), "reset_pct": int(m.group(2)),
           "ngl": int(m.group(4))}
    song = _SONG.match(m.group(3).strip())
    if song:
        out["song"] = song.group(1).strip()
        out["song_left"] = int(song.group(2))
    return out


def label(token):
    """Display name for a token, falling back to the raw token."""
    return EFFECTS.get(token, token)


def due(effects, cfg):
    """Which maintenance commands are warranted right now.

    `effects` is a parse_effects dict, `cfg` the guild `bard_maintain`
    block. Returns a list of (key, command) in the order they should be
    sent - `key` identifies the action for the caller's re-fire cooldown.

    A buff is due when its counter is below `threshold` OR when its token
    is missing from `effects` entirely (the I field being replace-not-merge
    means a lapsed buff vanishes rather than reading 0). Counters of None
    (`ab`, `RF`) are never due - there is nothing to compare.

    The three songs share one action: any of them low or missing warrants a
    single `cast refresh`, which tops up all three.
    """
    if not cfg or not cfg.get("enabled", True):
        return []
    limit = cfg.get("threshold", 30)
    out = []
    song_cmd = cfg.get("refresh_command")
    if song_cmd and any(_low(effects, t, limit) for t in SONGS):
        out.append(("refresh", song_cmd))
    for token, cmd in (cfg.get("spells") or {}).items():
        if cmd and _low(effects, token, limit):
            out.append((token, cmd))
    return out


def _low(effects, token, limit):
    if token not in effects:
        return True                  # lapsed entirely - see REPLACE note
    val = effects[token]
    return val is not None and val < limit
