"""katmud_lib.knight - 3s Knight decoders.

Sources (logs/gmcp_20261010_031731.log + the login readout):

  * The prompt, THREE lines:
        HP:1146/1146 | SP:2632/808 | STM:1484/1484 | STN:0%(1) | SW:100.0% | Neu Melee
        PPD(n) | BA(0) BC(N) | Stud(Y) Braw(n) | Dodg(n) Endu(Y) Defl(Y)
        [LF:0] [HMN:16.78784%] [GXN:267033] [MST:1086/1200]
  * `gs`: "Morale: 3/3".
  * GMCP Guild.State  mount_stamina(_max), mounted, mount_name - on change.

GAME FACTS: STM = stamina; STN = Strain, the gp2 pool, bad when HIGH;
SW = Second Wind (like the angel superpower consecrate); PPD = Prepared -
`prepare on` after gearing up gives a defensive bonus from worn gear, and is
easy to forget; BC = Battlecry, a glvl 70+ power; LF = rounds the last fight
took; MST = mount stamina. BA unknown. Melee is the default method; Charge
needs a higher glvl and a quest.
"""
import re

_STM_RE = re.compile(r"\bSTM:(\d+)/(\d+)")
_STN_RE = re.compile(r"\bSTN:(\d+)%")
_SW_RE = re.compile(r"\bSW:([\d.]+)%")
_STANCE_RE = re.compile(r"\|\s*(Neu|Off|Def)\w*\s+(\w+)\s*$")
_PPD_RE = re.compile(r"\bPPD\((\w)\)")
_BC_RE = re.compile(r"\bBC\((\w)\)")
_LF_RE = re.compile(r"\[LF:(\d+)\]")
_MST_RE = re.compile(r"\[MST:(\d+)/(\d+)\]")
_MORALE_RE = re.compile(r"^\s*Morale:\s*(\d+)/(\d+)")

_STANCES = {"Neu": "Neutral", "Off": "Offensive", "Def": "Defensive"}


def parse_line(line):
    """One prompt line (or the `gs` morale line) -> its fields, or None."""
    line = line or ""
    out = {}
    m = _STM_RE.search(line)
    if m:
        out["gp1"], out["gp1max"] = int(m.group(1)), int(m.group(2))
        m = _STN_RE.search(line)
        if m:
            out["gp2"] = int(m.group(1))
        m = _SW_RE.search(line)
        if m:
            out["sw"] = float(m.group(1))
        m = _STANCE_RE.search(line)
        if m:
            out["stance"] = _STANCES[m.group(1)]
            out["method"] = m.group(2)
        return out
    m = _PPD_RE.search(line)
    if m:
        out["prepared"] = m.group(1).lower() == "y"
        b = _BC_RE.search(line)
        if b:
            out["battlecry"] = b.group(1).lower() == "y"
        return out
    m = _MST_RE.search(line)
    lf = _LF_RE.search(line)
    if m or lf:
        if lf:
            out["lf"] = int(lf.group(1))
        if m:
            out["mount"], out["mountmax"] = int(m.group(1)), int(m.group(2))
        return out
    m = _MORALE_RE.match(line)
    if m:
        return {"morale": int(m.group(1)), "morale_max": int(m.group(2))}
    return None


def parse_gmcp(data):
    """Guild.State -> the mount fields it carries."""
    out = {}
    for src, dst in (("mount_stamina", "mount"),
                     ("mount_stamina_max", "mountmax")):
        v = data.get(src)
        if isinstance(v, int) and not isinstance(v, bool):
            out[dst] = v
    if isinstance(data.get("mounted"), int):
        out["mounted"] = bool(data["mounted"])
    if isinstance(data.get("mount_name"), str):
        out["mount_name"] = data["mount_name"]
    return out


def status_line(st):
    """`Neutral Melee  ·  UNPREPARED  ·  SW 100%  ·  Morale 3/3  ·  LF 0  ·
    Flash (mounted)`; anything never received is left out."""
    parts = []
    sm = " ".join(st[k] for k in ("stance", "method") if st.get(k))
    if sm:
        parts.append(sm)
    if "prepared" in st:
        parts.append("Prepared" if st["prepared"] else "UNPREPARED")
    if st.get("battlecry"):
        parts.append("Battlecry")
    if "sw" in st:
        parts.append(f"SW {st['sw']:g}%")
    if "morale" in st:
        parts.append(f"Morale {st['morale']}/{st.get('morale_max', '?')}")
    if "lf" in st:
        parts.append(f"LF {st['lf']}")
    if st.get("mount_name"):
        riding = "mounted" if st.get("mounted") else "not mounted"
        parts.append(f"{st['mount_name']} ({riding})")
    return "  ·  ".join(parts)
