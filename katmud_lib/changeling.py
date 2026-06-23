"""katmud_lib.changeling - 3s Changeling text decoders.

GUILD DISCIPLINE (necro spec 2.4): everything changeling-specific lives here
and is wired in only through muds/3s/guilds/changelings.json (the bar layout)
plus the `guild == "changelings"` branches in client.py.

Changelings have NO guild-specific MIP tag - their vitals ride the mud-wide
FFF feed (A=hp, C=sp). BUT the two guild pools, Protoplasm (gp1) and Stamina
(gp2), are NOT cleanly in FFF: the feed's `E` field actually carries Stamina,
yet the global composite map (protocol.COMPOSITE_NUM) assigns E->gp1, so
stamina would land in the Protoplasm bar and the real protoplasm value never
arrives (it only changes when you reform/heal, which FFF doesn't delta here).

The reliable source for BOTH pools is the custom prompt line, which always
prints them explicitly:

    HP[666/666] SP[191/192] ST[98.99%] PP[100.00%] CF[1/26%] FF[25.22%] E[] N PA

So this is a LINE/PROMPT parser (like necro.parse_prompt). PP = Protoplasm ->
gp1, ST = Stamina -> gp2, both percentages with max 100 (the bars read
gp1max/gp2max straight from vitals). HP/SP are parsed too as a steady fallback.
"""

import re

_PROMPT_HP = re.compile(r"HP\[(\d+)/(\d+)\]")
_PROMPT_SP = re.compile(r"SP\[(\d+)/(\d+)\]")
_PROMPT_ST = re.compile(r"ST\[([\d.]+)%\]")
_PROMPT_PP = re.compile(r"PP\[([\d.]+)%\]")


def is_prompt(line):
    """A changeling status prompt = HP[.../...] with the ST[..%] and PP[..%]
    pool readouts on the same line."""
    return "HP[" in line and "PP[" in line and "ST[" in line


def parse_prompt(line):
    """Changeling status prompt -> vitals dict. PP (Protoplasm) -> gp1, ST
    (Stamina) -> gp2, both as percentages with max 100 (bars read gp1max/
    gp2max from vitals directly). HP/SP carried too. {} if not a prompt."""
    if not is_prompt(line):
        return {}
    out = {}
    m = _PROMPT_HP.search(line)
    if m:
        out["hp"], out["hpmax"] = int(m.group(1)), int(m.group(2))
    m = _PROMPT_SP.search(line)
    if m:
        out["sp"], out["spmax"] = int(m.group(1)), int(m.group(2))
    m = _PROMPT_PP.search(line)
    if m:
        out["gp1"], out["gp1max"] = float(m.group(1)), 100.0
    m = _PROMPT_ST.search(line)
    if m:
        out["gp2"], out["gp2max"] = float(m.group(1)), 100.0
    return out


# --- the `forms` table -----------------------------------------------------
# A typed-command readout (not MIP). Two form cells per printed row, '|'
# separated, each cell: "<name>  <Fam:int>  [<Next Pt:float%>]". The capped
# form (Familiarity 30, e.g. Triceratops) prints no Next Pt. Example row:
#   | Bombardier Beetle     20    28.62% | Trap Door               1    78.50% |
FORM_CAP = 30                       # familiarity ceiling; capped = mastered
_FORM_CELL = re.compile(r"^(.+?)\s+(\d+)(?:\s+([\d.]+)%)?$")
FORMS_POINTS = re.compile(r"Available Points:\s*(\d+)")


def parse_forms_line(line):
    """Parse one printed row of the `forms` table into
    [(name, fam, next_pct_or_None), ...] - one tuple per form cell. Returns []
    for the header / separator / borders / any non-data line (so a caller can
    just try every line)."""
    if "|" not in line:
        return []
    out = []
    for cell in line.split("|"):
        cell = cell.strip()
        if not cell:
            continue
        m = _FORM_CELL.match(cell)
        if not m:
            return []           # a non-form cell -> this isn't a data row
        nxt = m.group(3)
        out.append((m.group(1).strip(), int(m.group(2)),
                    float(nxt) if nxt is not None else None))
    return out


def target_attack_form(forms, cap=FORM_CAP):
    """The form to morph into for LEVELLING: the highest familiarity strictly
    below `cap` (closest to mastering). `forms` is {name_lower: {"name","fam",
    "next"}}. Returns the display name, or None if every form is capped/absent.
    The capped form (fam >= cap, e.g. Triceratops) is reserved for survival and
    deliberately not returned here."""
    best = None
    for f in forms.values():
        if f["fam"] < cap and (best is None or f["fam"] > best["fam"]):
            best = f
    return best["name"] if best else None


# --- the FFF I-field (status string) ---------------------------------------
# Stripped form: "[N] [Chaos Flux: 1/53%]  [Density: 75.23%]  [FF(20): 29.07%]
# [Bombardier Beetle]". Last bracket = current form; FF(NN) = its familiarity.
_IFIELD_FORM = re.compile(r"\[([^\]]+)\]\s*$")
_IFIELD_FF = re.compile(r"\[FF\((\d+)\):")
_BIOPLASTS = re.compile(r"Bioplasts:\s*(\d+)")


def current_form(status):
    """Current form name from the stripped I-field (its last [bracket]), or
    None. 'Triceratops' would mean we're tanking, anything else = hunting."""
    m = _IFIELD_FORM.search(status or "")
    return m.group(1).strip() if m else None


def current_ff(status):
    """Current form's familiarity LEVEL from `FF(NN)` in the I-field, or None."""
    m = _IFIELD_FF.search(status or "")
    return int(m.group(1)) if m else None


def bioplasts(vitals):
    """Bioplast count from the FFF J-field (stored as vitals['hpbar2'], e.g.
    ' [Bioplasts: 2] [ADR] '). 0 if none/unknown."""
    m = _BIOPLASTS.search(str((vitals or {}).get("hpbar2", "")))
    return int(m.group(1)) if m else 0


# --- safety gate: rating range vs best kill --------------------------------
# rating prints "Monster class range since inception: 5,640 to 1,643,955";
# the plain `score` prints "Best kill: A cow (undead) (class: 404,370)" (the
# guild score's "Best Kill ... Class: N" is a DIFFERENT, higher number we do
# NOT use - the parens + lowercase 'class' here are what distinguish them).
_RATING_RANGE = re.compile(
    r"class range since inception:\s*([\d,]+)\s*to\s*([\d,]+)", re.I)
_BEST_KILL = re.compile(r"[Bb]est kill:.*\(class:\s*([\d,]+)\)")


def _to_int(s):
    return int(s.replace(",", ""))


def parse_rating_top(line):
    """Top (hardest) monster class from a rating range line, or None."""
    m = _RATING_RANGE.search(line)
    return _to_int(m.group(2)) if m else None


def parse_best_kill(line):
    """Best-kill class from the PLAIN `score` line, or None."""
    m = _BEST_KILL.search(line)
    return _to_int(m.group(1)) if m else None


# --- agent decision hooks (called via client.agent_hook) --------------------
def _stamina_pct(c):
    """Stamina (gp2) as a 0-100%, or 100 if unknown."""
    cur = c.vitals.get("gp2")
    top = c.vitals.get("gp2max") or 100
    if not cur or not top:
        return 100
    return int(100 * cur / top)


def agent_should_flee(c):
    """Leave the room when Protoplasm OR Stamina drops below the flee %."""
    return c._agent_pool_pct() < int(c.setting("agent_flee_pct", 30))


def agent_should_heal(c):
    """Act when stamina dips to the triceratops (tank) threshold, or when HP or
    a pool dips to the general heal threshold (the per-round auto-morph usually
    keeps HP up, so this mostly serves the stamina/tank trigger)."""
    return (_stamina_pct(c) < int(c.setting("agent_triceratops_pct", 60))
            or min(c._agent_hp_pct(), c._agent_pool_pct())
            < int(c.setting("agent_heal_pct", 50)))


def agent_low_hp(c, _losing):
    """Stamina is the changeling's real survival metric: morph the capped tank
    form (Triceratops) when stamina dips below agent_triceratops_pct and STAY
    there for the fight - agent_ensure_form swaps back to the levelling form
    once combat ends. Otherwise the routine HP heal: a bioplast if we have one,
    else a bare 'morph'. (auto-reform tops HP each round anyway.)"""
    if _stamina_pct(c) < int(c.setting("agent_triceratops_pct", 60)):
        cur = current_form(c.changeling_status)
        if cur and cur.lower() == "triceratops":
            return []                    # already tanking - don't re-morph
        return ["morph triceratops"]
    if bioplasts(c.vitals) > 0:
        return ["consume bioplast"]
    return ["morph"]


def agent_ensure_form(c):
    """Between fights, keep us in the levelling form. Returns 'morph <form>' if
    we're not already in the target form (and one is known), else None. The
    target is the highest-familiarity form still under the cap; if we're in
    Triceratops (post-survival) this morphs us back to it."""
    target = target_attack_form(c.changeling_forms)
    if not target:
        return None
    cur = current_form(c.changeling_status)
    if cur and cur.lower() == target.lower():
        return None
    return f"morph {target}"
