"""katmud_lib.mage - 3s Mage decoders.

Sources (logs/gmcp_20261010_031146.log + the login readout):

  * The prompt, TWO lines, every combat round and on `hp`:
        HP: 252/252 SP: 865/865/90% Umb: 100% Cnc: 0% B:2 I:6 S:1/24% G2N:36%
        GLR:0 A SW LS
    Umb = umbra %, Cnc = concentration %, B = bridges left (the default
    hpbar adds /reset%), I = imbues left, S = school spells left/reset%,
    then GLR (gxp last round) and the active protections. The layout is
    the player's own (`confhp`), so every field is optional.
  * GMCP Guild.State  bridge_reset_pct, imbue_reset_pct, magical_reset_pct
                      (the guild reset) - delta feed.
"""
import re

_UMB_RE = re.compile(r"\bUmb:\s*(\d+)%")
_CNC_RE = re.compile(r"\bCnc:\s*(\d+)%")
_BRIDGE_RE = re.compile(r"\bB:(\d+)(?:/(\d+)%)?")
_IMBUE_RE = re.compile(r"\bI:(\d+)")
_SCHOOL_RE = re.compile(r"\bS:(\d+)/(\d+)%")
_GLR_RE = re.compile(r"^\s*GLR:\s*\d+\s*(.*?)\s*$")

_GMCP_PCTS = {"bridge_reset_pct": "bridge_pct", "imbue_reset_pct": "imbue_pct",
              "magical_reset_pct": "reset_pct"}


def parse_prompt(line):
    """One prompt line -> its fields, or None. Line 1 is anchored on Umb:,
    line 2 on a leading GLR:."""
    line = line or ""
    m = _GLR_RE.match(line)
    if m:
        return {"protections": m.group(1)}
    m = _UMB_RE.search(line)
    if not m:
        return None
    out = {"gp1": int(m.group(1))}
    m = _CNC_RE.search(line)
    if m:
        out["gp2"] = int(m.group(1))
    m = _BRIDGE_RE.search(line)
    if m:
        out["bridges"] = int(m.group(1))
        if m.group(2):
            out["bridge_pct"] = int(m.group(2))
    m = _IMBUE_RE.search(line)
    if m:
        out["imbues"] = int(m.group(1))
    m = _SCHOOL_RE.search(line)
    if m:
        out["school"], out["school_pct"] = int(m.group(1)), int(m.group(2))
    return out


def parse_gmcp(data):
    """Guild.State -> the reset percentages it carries."""
    out = {}
    for src, dst in _GMCP_PCTS.items():
        v = data.get(src)
        if isinstance(v, int) and not isinstance(v, bool):
            out[dst] = v
    return out


def status_line(st):
    """`Bridge 2 (71%)  Imbue 6 (73%)  School 1 (24%)  Reset 26%  ·  A SW LS`;
    anything never received is left out."""
    parts = []
    for name, n, pct in (("Bridge", "bridges", "bridge_pct"),
                         ("Imbue", "imbues", "imbue_pct"),
                         ("School", "school", "school_pct")):
        if n in st:
            p = f" ({st[pct]}%)" if pct in st else ""
            parts.append(f"{name} {st[n]}{p}")
    if "reset_pct" in st:
        parts.append(f"Reset {st['reset_pct']}%")
    line = "  ".join(parts)
    if st.get("protections"):
        line = f"{line}  ·  {st['protections']}" if line \
            else st["protections"]
    return line
