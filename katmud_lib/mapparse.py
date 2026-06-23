"""katmud_lib.mapparse - 3Scapes room/exit MIP parsing (stage 3).

Pure functions, no I/O, no client/db imports beyond the shared ANSI
regex - so they can be tested in isolation (see --selftest) and reused
by the stage-4 charting layer.

Two MIP packets fire on every room change (muds/3s/mud.json):

  BAD (handler `room`)  - the room line. Three observed shapes:
      with exits:  "<desc> (<exits>) [<bracket>]~<vnum>"
      no exits:    "<desc>~<vnum>"
      ([<bracket>] is an optional MUD-side display setting; the exit
       group + bracket only appear together, and only when the room has
       visible exits.)
  DDD (handler `exits`) - "<exit>~<exit>~...~<vnum>"  (exits may be empty)

Canonical key: the trailing `~<vnum>`, present on BOTH packets for every
room (verified against live 3s captures, incl. no-exit rooms). The
printed `[bracket]` is NOT authoritative - it's a display toggle and can
carry a misconfigured prefix - so we key on the tilde id and treat the
bracket only as a cross-check.

Exit authority: DDD. The exit list in BAD's parens is freeform and can
be confused with parentheses that are genuinely part of the description
(spec: "may occasionally contain parentheses ... flag ambiguous
results"). When the bracket setting is off there is nothing structural
to tell a trailing "(locked)" in a description from a real "(n,e)" exit
group - so we only strip BAD's trailing parens from the stored
description when DDD confirms those tokens are the exits (or a [bracket]
confirms it's the id group). Otherwise the parens stay in the desc.
"""

import re
from collections import namedtuple

from .protocol import ANSI_RE

# A trailing ~<digits> = the canonical room id.
_TILDE_VNUM_RE = re.compile(r"~(\d+)\s*$")
# A trailing "(<exits>) [<bracket>]" group (bracket optional), at end of
# the string once the ~<vnum> has been removed.
_EXIT_TAIL_RE = re.compile(r"\(([^()]*)\)\s*(?:\[(\d+)\])?\s*$")

# parse_room: structural read of one BAD packet.
ParsedRoom = namedtuple("ParsedRoom", "vnum exits short_desc bracket_vnum")
# parse_exits: structural read of one DDD packet.
ParsedExits = namedtuple("ParsedExits", "vnum exits")
# parse_packets: reconciled view for the charting layer. warnings is a
# (possibly empty) list of human-readable anomalies to surface, not bury.
RoomObservation = namedtuple("RoomObservation", "vnum exits short_desc warnings")


def _clean(s):
    return ANSI_RE.sub("", s).strip()


def _split_commas(s):
    return [t.strip() for t in s.split(",") if t.strip()]


def parse_exits(ddd_payload):
    """Parse a DDD packet. Returns ParsedExits(vnum, exits); vnum is None
    if the packet has no trailing integer (malformed)."""
    toks = _clean(ddd_payload).split("~")
    if toks and toks[-1].isdigit():
        vnum, exit_toks = int(toks[-1]), toks[:-1]
    else:
        vnum, exit_toks = None, toks   # no id: keep every token as an exit
    exits = [t.strip() for t in exit_toks if t.strip()]
    return ParsedExits(vnum, exits)


def parse_room(bad_payload, known_exits=None):
    """Parse a BAD packet. Returns ParsedRoom, or None if there is no
    trailing ~<vnum> to key on (defensive - should not happen on the wire
    since BAD is a protocol packet, not the wrappable printed line).

    known_exits (the authoritative DDD exit list) disambiguates a trailing
    parenthesised group: the group is stripped from short_desc only when a
    [bracket] confirms it is the id group, or its tokens match known_exits.
    """
    s = _clean(bad_payload)
    m = _TILDE_VNUM_RE.search(s)
    if not m:
        return None
    vnum = int(m.group(1))
    head = s[:m.start()].rstrip()
    exits = list(known_exits) if known_exits is not None else []
    bracket = None
    em = _EXIT_TAIL_RE.search(head)
    if em:
        paren_exits = _split_commas(em.group(1))
        bracket = int(em.group(2)) if em.group(2) is not None else None
        confirmed = bracket is not None or (
            known_exits is not None and paren_exits == list(known_exits))
        if confirmed:
            head = head[:em.start()].rstrip()
            if known_exits is None:
                exits = paren_exits
    return ParsedRoom(vnum, exits, head, bracket)


def is_truncated_id(tilde, bracket):
    """3Scapes sometimes truncates the tilde `~<vnum>` to FEWER digits than
    the room's real id (it drops leading digit(s)), while the optional
    [bracket] - the in-game room-id display - carries the FULL id. The
    signature: the bracket is larger than the tilde AND the tilde is a
    trailing-digit suffix of the bracket. Examples (all real or live):
    [150950]~50950 -> 150950, [1774]~774 -> 1774, [1277]~277 -> 1277. When
    this holds the bracket is authoritative (confirmed live: the player
    stands in the bracketed room, and mapdetail showing the truncated id was
    the bug). A bracket that is NOT a suffix-superset of the tilde (e.g.
    [999]~277) is a genuine display misconfiguration - flagged, not trusted."""
    if tilde is None or bracket is None:
        return False
    return bracket > tilde and str(bracket).endswith(str(tilde))


def parse_packets(bad_payload, ddd_payload):
    """Reconcile a BAD + DDD pair into one RoomObservation for charting.
    Exits come from DDD (authoritative); desc from BAD with the exit/id
    tail stripped; vnum from BAD, cross-checked against DDD and the
    bracket. The bracket also RECOVERS the full id when 3s truncated the
    tilde to 5 digits (see is_truncated_id). Returns None if BAD has no
    keyable id."""
    pe = parse_exits(ddd_payload)
    pr = parse_room(bad_payload,
                    known_exits=pe.exits if pe.vnum is not None else None)
    if pr is None:
        return None
    warnings = []
    # BAD vs DDD: both tildes are the same (possibly truncated) low-5 id,
    # so compare them directly - a mismatch here is a genuine pairing slip.
    if pe.vnum is not None and pe.vnum != pr.vnum:
        warnings.append(
            f"vnum mismatch: BAD ~{pr.vnum} vs DDD ~{pe.vnum}")
    vnum = pr.vnum
    if is_truncated_id(pr.vnum, pr.bracket_vnum):
        vnum = pr.bracket_vnum          # full id, recovered from the bracket
    elif pr.bracket_vnum is not None and pr.bracket_vnum != pr.vnum:
        warnings.append(
            f"bracket [{pr.bracket_vnum}] != id ~{pr.vnum} "
            "(bracket display misconfigured?)")
    exits = pe.exits if pe.vnum is not None else pr.exits
    return RoomObservation(vnum, exits, pr.short_desc, warnings)


# ==========================================================================
# Standalone self-test: python -m katmud_lib.mapparse --selftest
# ==========================================================================
def _selftest():
    # (bad, ddd, vnum, exits, short_desc) - the three live 3s captures.
    live = [
        ("Houston - Midway Shuttlecraft~6312",
         "6312", 6312, [], "Houston - Midway Shuttlecraft"),
        ("The Houston-Midways shuttle bay (w) [6314]~6314",
         "w~6314", 6314, ["w"], "The Houston-Midways shuttle bay"),
        ("Entrance (sci,shop,w,leave,chaos,craft,magic,n,guild,gypsy) "
         "[409]~409",
         "sci~shop~w~leave~chaos~craft~magic~n~guild~gypsy~409",
         409,
         ["sci", "shop", "w", "leave", "chaos", "craft", "magic", "n",
          "guild", "gypsy"],
         "Entrance"),
    ]
    for bad, ddd, vnum, exits, desc in live:
        obs = parse_packets(bad, ddd)
        assert obs is not None, bad
        assert obs.vnum == vnum, (bad, obs.vnum)
        assert obs.exits == exits, (bad, obs.exits)
        assert obs.short_desc == desc, (bad, repr(obs.short_desc))
        assert obs.warnings == [], (bad, obs.warnings)

    # Description ending in non-exit parens, bracket OFF, DDD = no exits:
    # the parens must stay in the description, exits stay empty.
    obs = parse_packets("A small room (locked)~700", "700")
    assert obs.vnum == 700 and obs.exits == []
    assert obs.short_desc == "A small room (locked)", obs.short_desc

    # Same but the room really has exits (bracket OFF): DDD confirms the
    # tail tokens are exits, so they are stripped from the description
    # while a genuine desc-paren earlier in the line is preserved.
    obs = parse_packets("A dark cave (drippy) (n,e)~701", "n~e~701")
    assert obs.vnum == 701 and obs.exits == ["n", "e"]
    assert obs.short_desc == "A dark cave (drippy)", obs.short_desc

    # ANSI colour in the payload is stripped from the stored desc.
    obs = parse_packets("\x1b[35;1mGuild Hall\x1b[0m (n) [42]~42", "n~42")
    assert obs.short_desc == "Guild Hall", repr(obs.short_desc)
    assert obs.vnum == 42 and obs.exits == ["n"]

    # Bracket carries the full id while the tilde dropped its leading digit
    # (live 3s case [1774]~774 generalised): the bracket is a suffix-superset
    # of the tilde, so it is authoritative and recovers the full id, no warn.
    obs = parse_packets("elbow joint (w,n,s) [1277]~277", "w~n~s~277")
    assert obs.vnum == 1277 and obs.exits == ["w", "n", "s"]
    assert obs.short_desc == "elbow joint"
    assert obs.warnings == [], obs.warnings

    # A genuinely misconfigured bracket (NOT a suffix-superset of the tilde)
    # is surfaced as a warning and the tilde stays authoritative.
    obs = parse_packets("broom closet (n) [999]~277", "n~277")
    assert obs.vnum == 277 and obs.exits == ["n"]
    assert any("bracket" in w for w in obs.warnings), obs.warnings

    # A genuine id disagreement between the two packets is flagged.
    obs = parse_packets("Foo (n) [9]~9", "n~10")
    assert any("vnum mismatch" in w for w in obs.warnings), obs.warnings

    # 3s truncates the tilde id to 5 digits; the bracket carries the full
    # 6-digit id, so [150950]~50950 -> 150950 with no warning (real wire
    # capture: "The inner courtyard (e,w,n,s) [150950]~50950").
    obs = parse_packets("The inner courtyard (e,w,n,s) [150950]~50950",
                        "e~w~n~s~50950")
    assert obs.vnum == 150950, obs.vnum
    assert obs.exits == ["e", "w", "n", "s"], obs.exits
    assert obs.short_desc == "The inner courtyard", repr(obs.short_desc)
    assert obs.warnings == [], obs.warnings
    assert is_truncated_id(50950, 150950)
    assert is_truncated_id(277, 1277)          # leading digit dropped
    assert is_truncated_id(774, 1774)          # the live 4-digit case
    assert not is_truncated_id(277, 999)       # not a suffix -> misconfig
    assert not is_truncated_id(277, 277)       # equal -> no truncation

    # Defensive: a BAD payload with no trailing id can't be keyed.
    assert parse_room("a line that lost its id") is None
    # Malformed DDD (no trailing integer) -> vnum None, exits parsed.
    pe = parse_exits("n~e~s")
    assert pe.vnum is None and pe.exits == ["n", "e", "s"]

    print("OK")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: python -m katmud_lib.mapparse --selftest")
