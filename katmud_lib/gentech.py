"""katmud_lib.gentech - 3s Gentech decoders.

MIP's FFF feed carried PU/SPU and the hpbar1/hpbar2 status text until MIP
was removed mud-side on 2026-09-15. What replaces it
(logs/gmcp_20261004_015932.log + logs/random-20261004.log):

  * GMCP Guild.State   cpc, reset_pct, efield_mins, edna_secs
    GMCP Guild.Systems tactical_secs, stabilize_secs
    GMCP Guild.Progress rested_secs
    All DELTA feeds - a field is only sent when it changes (cpc stops
    arriving once it sits at its max).
  * PU and SPU appear in NO GMCP package. The hpbar is their only source,
    and it prints every COMBAT round (plus on `hp` / `reclaim hp`), so the
    bars hold still out of combat.

The hpbar is `genset hpbar`-customizable, so fields are found by label:

    default: HP:4326/4326 SP:282/282 PU:9450/9450(4725) CPC:10000/10000
             [G2N:25%][45%] E:none
    custom:  ... G2N:747626 RC:14180293 GenEff:100 TechEff:100 E:none

`PU:cur/max(store)` - store is SPU's current value.

GAME FACTS (user, 2026-10-04): SPU max is half of PU max (9450 -> 4725,
matching `gs`), and CPC max is 10000.
"""
import re

from . import blade

# `\s*` after each colon: a custom bar is free to write `PU: 9225/...`
# (the user's own, 2026-10-04) where the default writes `PU:9450/...`.
_PU_RE = re.compile(r"\bPU:\s*(\d+)/(\d+)\((\d+)\)")
_CPC_RE = re.compile(r"\bCPC:\s*(\d+)/")
_EFF_RE = re.compile(r"\b(GenEff|TechEff):\s*(\d+)")
CPC_MAX = 10000

# GMCP field -> state key. The timers are WIRE-CONFIRMED in the capture.
GMCP_FIELDS = ("cpc", "reset_pct", "efield_mins", "edna_secs",
               "tactical_secs", "stabilize_secs", "rested_secs")
# UNCONFIRMED: GenEff/TechEff have never been seen in GMCP - the capture
# was taken while Stabilize held both at 100, and a delta feed sends
# nothing for an unchanged value. These names are a placeholder until a
# capture shows the real ones; a wrong name means GMCP never updates them.
GMCP_EFF_FIELDS = {"geneff": "geneff", "techeff": "techeff"}


def parse_hpbar(line):
    """hpbar text -> dict of what it carries, or None if it is not one.

    PU (gp1/gp1max/gp2) is the anchor; CPC and GenEff/TechEff are optional
    because a custom hpbar may leave them out (the default has no GenEff)."""
    m = _PU_RE.search(line or "")
    if not m:
        return None
    pu, pumax, spu = map(int, m.groups())
    out = {"gp1": pu, "gp1max": pumax, "gp2": spu, "gp2max": pumax // 2}
    c = _CPC_RE.search(line)
    if c:
        out["cpc"] = int(c.group(1))
    for label, val in _EFF_RE.findall(line):
        out["geneff" if label == "GenEff" else "techeff"] = int(val)
    return out


def parse_hpbar_eff(line):
    """GenEff/TechEff from a later hpbar line (the custom bar wraps them
    onto line 2, which has no PU: anchor)."""
    return {("geneff" if label == "GenEff" else "techeff"): int(val)
            for label, val in _EFF_RE.findall(line or "")}


def _dur(secs):
    """Same shape as MudClient._fmt_duration: days, else blade.fmt_secs."""
    d, rem = divmod(int(secs), 86400)
    if d:
        return f"{d}d{rem // 3600}h"
    return blade.fmt_secs(rem)


def status_line(st):
    """The info-pane line. Anything never received is left out rather
    than shown as a guess."""
    parts = []
    if "cpc" in st:
        parts.append(f"CPC {st['cpc']}/{CPC_MAX}")
    if "reset_pct" in st:
        parts.append(f"Reset {st['reset_pct']}%")
    for key, label, mult in (("edna_secs", "Edna", 1),
                             ("efield_mins", "Efield", 60),
                             ("tactical_secs", "Tact", 1),
                             ("stabilize_secs", "Stab", 1),
                             ("rested_secs", "Rested", 1)):
        if key in st:
            parts.append(f"{label} {_dur(st[key] * mult)}")
    if "geneff" in st:
        parts.append(f"Gen {st['geneff']}%")
    if "techeff" in st:
        parts.append(f"Tech {st['techeff']}%")
    return "  ".join(parts)
