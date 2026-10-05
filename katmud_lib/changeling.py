"""katmud_lib.changeling - 3s Changeling text decoders.

GUILD DISCIPLINE (necro spec 2.4): everything changeling-specific lives here
and is wired in only through muds/3s/guilds/changelings.json (the bar layout)
plus the `guild == "changelings"` branches in client.py.

MIP/FFF is gone mud-side (2026-09-15). The sources now
(logs/gmcp_20261004_212515.log):

  * The prompt, every round and on `hp` - the only source of Protoplasm:
        HP[666/666] SP[191/192] ST[98.99%] PP[100.00%] CF[1/26%] FF[25.22%] E[] N PA
    PP = Protoplasm -> gp1, ST = Stamina -> gp2, both % with max 100.
  * GMCP Guild.State, a delta feed: `stamina` (same value as ST, and it
    keeps ticking through regen between fights), `bioplasts` (count
    carried, on change), `kills`, `super` (meaning unknown).
  * GMCP Guild.Info: `current_form` / `form_group` on a morph only.
  * `gs`:  | Form        : Triceratops            Form Points : 69 |
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


# --- the status line (prompt + GMCP, since MIP/FFF went 2026-09-15) --------
# Prompt:  HP[666/666] SP[190/192] ST[89.78%] PP[100.00%] CF[1/86%] FF[MAX]
#          E[] N PA
# CF = Chaos Flux, FF = the current form's familiarity % (MAX when capped),
# the letter after E[..] = the chaotic forces (gh confighp: D detrimental,
# N neutral, B beneficial), then the active intrinsics (P = Perform,
# A = Adrenalize). GMCP Guild.Info sends current_form only on a
# morph; Guild.State sends bioplasts (the count carried) only on a change.
_PROMPT_CF = re.compile(r"CF\[([^\]]*)\]")
_PROMPT_FF = re.compile(r"FF\[([^\]]*)\]")
_PROMPT_CHAOS = re.compile(r"E\[[^\]]*\]\s+([NDB])\b")
_GS_FORM = re.compile(r"\|\s*Form\s+:\s+(.+?)\s{2,}Form Points")
_PROMPT_INTRINSICS = re.compile(r"E\[[^\]]*\]\s+[NDB]\b *([A-Za-z]*)")
CHAOS = {"N": "Neutral", "D": "Detrimental", "B": "Beneficial"}
INTRINSICS = {"P": "Perform", "A": "Adrenalize"}


def parse_prompt_status(line):
    """Flux / FF / chaos letter off the prompt, or {} if not a prompt."""
    if not is_prompt(line):
        return {}
    out = {}
    for key, rx in (("flux", _PROMPT_CF), ("ff", _PROMPT_FF),
                    ("chaos", _PROMPT_CHAOS)):
        m = rx.search(line)
        if m:
            out[key] = m.group(1)
    m = _PROMPT_INTRINSICS.search(line)
    if m:
        out["intrinsics"] = m.group(1)   # "" when none are active
    return out


def parse_gs_form(line):
    """`gs` row '| Form : Triceratops   Form Points : 69 |' -> form name."""
    m = _GS_FORM.search(line or "")
    return m.group(1).strip() if m else None


def parse_gmcp(data):
    """Guild.State / Guild.Info -> {stamina, bioplasts, form}, whichever
    this packet carries (both are delta feeds)."""
    out = {}
    v = data.get("stamina")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        out["stamina"] = float(v)
    v = data.get("bioplasts")
    if isinstance(v, int) and not isinstance(v, bool):
        out["bioplasts"] = v
    v = data.get("current_form")
    if isinstance(v, str) and v:
        out["form"] = v
    return out


def status_line(st):
    """The status-bar line; anything never received is left out."""
    parts = []
    if st.get("form"):
        form = st["form"].title()
        parts.append(f"{form}  FF {st['ff']}" if "ff" in st else form)
    elif "ff" in st:
        parts.append(f"FF {st['ff']}")
    if "flux" in st:
        parts.append(f"Flux {st['flux']}")
    if "chaos" in st:
        parts.append(CHAOS.get(st["chaos"], st["chaos"]))
    parts.extend(INTRINSICS.get(c, c) for c in st.get("intrinsics", ""))
    return "  ".join(parts)


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
        cur = c.changeling.get("form")
        if cur and cur.lower() == "triceratops":
            return []                    # already tanking - don't re-morph
        return ["morph triceratops"]
    if c.changeling.get("bioplasts", 0) > 0:
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
    cur = c.changeling.get("form")
    if cur and cur.lower() == target.lower():
        return None
    return f"morph {target}"
