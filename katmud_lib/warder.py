"""katmud_lib.warder - 3s Warder decoders.

Warders have no GP1/GP2 pools. GAME FACT: their superpower is STS
(N uses per reset), and Favor and Boil are secondary powers that share
one usage count. So the gp1 bar is STS and the gp2 bar is Favor/Boil.

Sources (logs/gmcp_20261004_102141.log + the session text):

  * The prompt, every combat round and on `hp`:
        HP: 697/697  SP: 266/266  STS: 3/14%  (V) (B) (P:E*A) Enemy: bruises
    `STS: count/pct%` - count is STS uses left (text-only), pct is the
    reset cycle (equals Guild.Info next_reset). (V) = Void up, (B) =
    blocking, P: = the Aes Sedai's pwards.
  * `gs`:  STS: 3/3(1%)   and   Favors/Boils: 2
  * `score`:  Gp2  :     2 / 2   (Favor/Boil cur/max; Gp1 reads None)
  * GMCP Guild.Info - a delta feed: next_reset, pwards (on change),
    gxp {total, to_spend, ...} every round. `boils {current, max}` is in
    `gh gmcp` but never arrived in the capture.

Favor/Boil does not appear on the prompt, so without GMCP `boils` the bar
only moves on `gs`/`score`.
"""
import re

_PROMPT_RE = re.compile(r"\bSTS:\s*(\d+)/(\d+)%")
_WARDS_RE = re.compile(r"\(P:([^)]*)\)")
_GS_STS_RE = re.compile(r"\bSTS:\s*(\d+)/(\d+)\((\d+)%\)")
_GS_BOILS_RE = re.compile(r"\bFavors/Boils:\s*(\d+)")
_SCORE_GP2_RE = re.compile(r"\bGp2\s*:\s*(\d+)\s*/\s*(\d+)")


def parse_prompt(line):
    """Prompt -> {sts, reset_pct, void, block, wards}, or None. `STS:` is
    the anchor; Void/Block/wards are read as absent when not shown."""
    line = line or ""
    m = _PROMPT_RE.search(line)
    if not m:
        return None
    w = _WARDS_RE.search(line)
    return {"sts": int(m.group(1)), "reset_pct": int(m.group(2)),
            "void": "(V)" in line, "block": "(B)" in line,
            "wards": w.group(1) if w else ""}


def parse_gs(line):
    """One `gs` or `score` line -> whatever it carries, or {}."""
    line = line or ""
    m = _GS_STS_RE.search(line)
    if m:
        return {"sts": int(m.group(1)), "sts_max": int(m.group(2)),
                "reset_pct": int(m.group(3))}
    m = _GS_BOILS_RE.search(line)
    if m:
        return {"boils": int(m.group(1))}
    m = _SCORE_GP2_RE.search(line)
    if m:
        return {"boils": int(m.group(1)), "boils_max": int(m.group(2))}
    return {}


def status_line(st):
    """The status-bar line; anything never received is left out."""
    parts = []
    if "sts" in st:
        mx = f"/{st['sts_max']}" if "sts_max" in st else ""
        pct = f" ({st['reset_pct']}%)" if "reset_pct" in st else ""
        parts.append(f"STS {st['sts']}{mx}{pct}")
    elif "reset_pct" in st:
        parts.append(f"Reset {st['reset_pct']}%")
    if st.get("void"):
        parts.append("Void")
    if st.get("block"):
        parts.append("Block")
    if st.get("wards"):
        parts.append(f"Wards {st['wards']}")
    if "gxp_spend" in st:
        parts.append(f"GXP to spend {st['gxp_spend']:,}")
    if "gxp_last" in st:
        parts.append(f"Last {st['gxp_last']:+,}")
    return "  ".join(parts)
