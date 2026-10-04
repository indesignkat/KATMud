"""katmud_lib.monk - 3s Monk decoders.

Chi (gp1) and Peace (gp2) rode MIP's FFF feed until MIP was removed
mud-side on 2026-09-15. What replaces it (logs/gan-20261004.log +
logs/gmcp_20261004_030513.log):

  * GMCP Guild.State  chi_points, peace, peace_level, ancient_energy_reset
                      (AE recharge %), gxp_to_spend - DELTA feed, a field
                      only arrives when it changes. chi_points ticks out of
                      combat too (it drains while meditating).
  * GMCP Guild.Info   chi_focus, combat_method - sent on change only, so
                      `gs` supplies them until the first change.
  * The prompt, every combat round and on `hp`:
        HP: 423/423 PP: 522/522 Chi: 228/282 G2:4343069 M:634 AE:10/36% CF:S
    `Chi: cur/max` is the only place Chi's max appears; `M:` is Peace;
    `AE:count/pct%` - the remaining COUNT is text-only.

GAME FACT (user, 2026-10-04): Peace has no known maximum - it can get very
high - so the bar shows the number alone and warns below an absolute 100
(monks.json), scaled against the highest value seen.
"""
import re

_CHI_RE = re.compile(r"\bChi:\s*(\d+)/(\d+)")
_PEACE_RE = re.compile(r"\bM:\s*(\d+)")
_AE_RE = re.compile(r"\bAE:\s*(\d+)/(\d+)%")
# gs lines
_GS_METHOD_RE = re.compile(r"\bCombat Method:\s*(\w+).*\bChi Focus:\s*(\w+)")
_GS_AE_RE = re.compile(r"\bAncient Energy Remaining\s*:\s*(\d+)\s+"
                       r"AE is (\d+)% recharged")
_GS_CHI_RE = re.compile(r"\bChi Points:\s*(\d+)\((\d+)\)")
_GS_PEACE_RE = re.compile(r"\bPeace Level:\s*(.*?)\((\d+)/\d+\)")


def parse_prompt(line):
    """Prompt -> {gp1, gp1max, gp2?, ae?, ae_pct?}, or None. Chi is the
    anchor; the rest are optional."""
    m = _CHI_RE.search(line or "")
    if not m:
        return None
    out = {"gp1": int(m.group(1)), "gp1max": int(m.group(2))}
    p = _PEACE_RE.search(line)
    if p:
        out["gp2"] = int(p.group(1))
    a = _AE_RE.search(line)
    if a:
        out["ae"], out["ae_pct"] = int(a.group(1)), int(a.group(2))
    return out


def parse_gs(line):
    """One `gs` line -> whatever it carries, or {}."""
    line = line or ""
    m = _GS_METHOD_RE.search(line)
    if m:
        return {"method": m.group(1), "focus": m.group(2)}
    m = _GS_AE_RE.search(line)
    if m:
        return {"ae": int(m.group(1)), "ae_pct": int(m.group(2))}
    m = _GS_CHI_RE.search(line)
    if m:
        return {"gp1": int(m.group(1)), "gp1max": int(m.group(2))}
    m = _GS_PEACE_RE.search(line)
    if m:
        return {"peace_level": m.group(1).strip(), "gp2": int(m.group(2))}
    return {}


def status_line(st):
    """The status-bar line; anything never received is left out. The
    peace text is NOT here - it stays in the info pane (user's layout)."""
    parts = []
    mf = " / ".join(st[k] for k in ("method", "focus") if st.get(k))
    if mf:
        parts.append(mf)
    if "ae" in st:
        pct = f" ({st['ae_pct']}%)" if "ae_pct" in st else ""
        parts.append(f"AE {st['ae']}{pct}")
    elif "ae_pct" in st:
        parts.append(f"AE {st['ae_pct']}%")
    if "gxp_spend" in st:
        parts.append(f"GXP to spend {st['gxp_spend']:,}")
    return "  ".join(parts)
