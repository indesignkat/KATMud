"""katmud_lib.necro - 3s Necromancer text decoders + the Track tracker.

GUILD DISCIPLINE (spec 2.4): everything necro-specific lives here and is
wired in only through muds/3s/guilds/necromancers.json (the vitals bar
layout) plus the `guild == "necromancers"` branches in client.py.

Necros have NO guild-specific MIP tag - their live data rides the mud-wide
FFF vitals feed (numeric hp/sp/np + the `I` hpbar text) and, for the things
that matter most, plain TEXT readouts the player types:

  * `powers` - the memorized-powers table; each `name(globes): uses total`
    row gives the REMAINING USES of a power (what the player tracks, so they
    know when to go re-memorize before a power runs dry).
  * `gs` (guild score) - the reagent stock lines (`ginseng:1095  ...`).
  * `inv` - corpse counts (on you / on follower / smuggled) - format TBD.

So these are LINE parsers, not MIP parsers. Power-firing decrements and the
inventory/corpse format await live examples; the parsers here cover the
readouts we have samples for.
"""

import re

# Canonical power names (lowercase, as the memorized table prints them).
# Drives the Track tracker's "is this a power?" test, so a tracked power
# that drops out of a fresh `powers` readout can be zeroed rather than left
# reading its last (stale) value.
NECRO_POWERS = {
    "sense", "create light", "create darkness", "turn undead",
    "funeral pyre", "dispell", "curse", "create dart", "protection",
    "animate dead", "mirror", "daemon graft", "spiritgrasp", "cryptwalk",
    "unholy ground", "revenant", "di/nocturnum", "corpselight", "requiem",
    "corpsecion", "alarm", "fright", "drain", "projection", "clutch",
    "scythe", "dream", "chest", "bloodforge", "soulbind", "deathwatch",
    "shroud", "serenity", "grim harvest",
}

# Reagents, as the gs screen prints them (multi-word names included).
NECRO_REAGENTS = {
    "ginseng", "black pearls", "spider web", "goldenrod", "mandrake",
    "pine needles", "nightshade", "bloodmoss",
}

# One memorized-powers row entry: `name(globes_each): uses globes_total`.
# uses = remaining memorized usages (the tracked number). Names may carry
# spaces ("animate dead") or a slash ("di/nocturnum").
_POWER_RE = re.compile(r"([a-z][a-z/ ]*?)\((\d+)\):\s*(\d+)\s+(\d+)")
# A `name: count` reagent pair off a gs line.
_REAGENT_RE = re.compile(r"([a-z][a-z ]*?):\s*(\d+)")

POWERS_HEADER = "powers memorized this cycle"
POWERS_FOOTER = "For more on any power"


def parse_powers_line(line):
    """Memorized-powers row -> {power: remaining_uses} for the (0, 1 or 2)
    entries on the line. Empty when the line isn't a powers row."""
    out = {}
    for name, _globes, uses, _total in _POWER_RE.findall(line):
        out[name.strip()] = int(uses)
    return out


def parse_reagents_line(line):
    """gs reagent line -> {reagent: count} for the known reagents on it.
    Restricted to NECRO_REAGENTS so unrelated `word: number` text (worth,
    timers, ...) can't masquerade as a reagent."""
    out = {}
    for name, count in _REAGENT_RE.findall(line):
        name = name.strip()
        if name in NECRO_REAGENTS:
            out[name] = int(count)
    return out


def parse_powers_block(text):
    """Whole `powers` readout -> {power: remaining_uses}. Convenience for
    tests / one-shot parsing; the client scans line-by-line live."""
    out = {}
    for line in text.splitlines():
        out.update(parse_powers_line(line))
    return out


# --- hpbar I-field (FFF composite hpbar1 text) ------------------------
# Wire sample (logs/mip_20260614_122626), the MIP feed that's ALWAYS on
# (no need to pipe the text hpbar to the main window):
#   <vC:1>  <gWorth:125%>  Tport:5/5  <gBlaz:9/9>  <gProt:ONX>  Circle:...
# The leading letter of each <...> tag is a COLOR (g=green/y=yellow=warn),
# and strip_mip_colors turns `<gWorth:125%>` into `Worth:125%`, so we parse
# the STRIPPED text. This MIP feed carries worth / protection / glamor /
# tport / circle - but NOT reset%, corpses, or veil (`vC` is a counter, not
# veil). Those three live ONLY in the full text hpbar prompt (parse_prompt).
def parse_hpbar(stripped):
    """Necro FFF hpbar1 text, AFTER strip_mip_colors -> {worth, protection,
    glamor, tport, circle} for the fields present."""
    out = {}
    m = re.search(r"Worth:\s*(-?\d+)", stripped)
    if m:
        out["worth"] = int(m.group(1))
    m = re.search(r"Prot:\s*(\w+)", stripped)
    if m:
        out["protection"] = m.group(1).upper().startswith("ON")
    m = re.search(r"Blaz:\s*(\S+)", stripped)
    if m:
        out["glamor"] = m.group(1)
    m = re.search(r"Tport:\s*(\S+)", stripped)
    if m:
        out["tport"] = m.group(1)
    m = re.search(r"Circle:\s*(.+?)\s*$", stripped)
    if m:
        out["circle"] = m.group(1).strip()
    return out


# --- text prompt -------------------------------------------------------
# The in-game prompt prints in the main output (user-confirmed) and is the
# authoritative source for the guild Status fields - it carries everything
# the FFF I-field does PLUS reset% and the corpse count:
#   HP[1114(118)/1114] SP[652(118)/652] NP[3700/3700|0c] E[none]
#   Status[w125%|pOFF|vON|r57%] Cr[You are miles from advancement.]
_PROMPT_HP = re.compile(r"HP\[(\d+)(?:\(\d+\))?/(\d+)\]")
_PROMPT_SP = re.compile(r"SP\[(\d+)(?:\(\d+\))?/(\d+)\]")
_PROMPT_NP = re.compile(r"NP\[(\d+)/(\d+)\|(\d+)c\]")
_PROMPT_E = re.compile(r"E\[([^\]]*)\]")
_PROMPT_STATUS = re.compile(r"Status\[([^\]]*)\]")
_PROMPT_CR = re.compile(r"Cr\[([^\]]*)\]")


def is_prompt(line):
    """A necro status prompt = a Status[...] and Cr[...] on one line."""
    return "Status[" in line and "Cr[" in line


def parse_prompt(line):
    """Necro status prompt -> dict. Numeric vitals (hp/sp/np + max) feed the
    bars (NP especially - FFF rarely sends it); corpses/worth/protection/
    veil/reset/circle are the guild Status fields. Returns {} if not a
    prompt."""
    if not is_prompt(line):
        return {}
    out = {}
    m = _PROMPT_HP.search(line)
    if m:
        out["hp"], out["hpmax"] = int(m.group(1)), int(m.group(2))
    m = _PROMPT_SP.search(line)
    if m:
        out["sp"], out["spmax"] = int(m.group(1)), int(m.group(2))
    m = _PROMPT_NP.search(line)
    if m:
        out["np"], out["npmax"] = int(m.group(1)), int(m.group(2))
        out["corpses"] = int(m.group(3))
    m = _PROMPT_E.search(line)
    if m:
        out["enemy"] = m.group(1).strip()
    m = _PROMPT_STATUS.search(line)
    if m:
        for tok in m.group(1).split("|"):
            tok = tok.strip()
            if not tok:
                continue
            key, val = tok[0], tok[1:]
            if key == "w":
                d = re.search(r"-?\d+", val)
                if d:
                    out["worth"] = int(d.group())
            elif key == "p":
                out["protection"] = val.upper().startswith("ON")
            elif key == "v":
                out["veil"] = val.upper().startswith("ON")
            elif key == "r":
                d = re.search(r"-?\d+", val)
                if d:
                    out["reset"] = int(d.group())
    m = _PROMPT_CR.search(line)
    if m:
        out["circle"] = m.group(1).strip()
    return out


# --- inventory corpse counts ------------------------------------------
# `inv` footer lines, each: `<Section>  [used/max| pct%|  Nc]`. The corpse
# count is the trailing `Nc`. Three carriers (sample 2026-06-15):
#   Encumberance -> on you, Grimare -> in the grimoire, Smuggling -> smuggled.
_INV_SECTIONS = {"Encumberance": "corpses", "Grimare": "grimare",
                 "Smuggling": "smuggled"}
_INV_RE = re.compile(
    r"^(Encumberance|Grimare|Smuggling)\s+\[\s*\d+/\s*\d+\|\s*\d+%\|"
    r"\s*(\d+)c\]")


def parse_inv_line(line):
    """An `inv` footer line -> {tracked_name: corpse_count}, or {}."""
    m = _INV_RE.search(line)
    if not m:
        return {}
    return {_INV_SECTIONS[m.group(1)]: int(m.group(2))}


# --- tier advancement (max-Circle Cr[...] messages) -------------------
# Once a necro hits max Circle, Cr[...] stops being a % and cycles these
# wordy messages toward the next tier. Ordered 0 (just started) -> last
# (ready). tier_progress maps a message to (index, total, pct).
TIER_MESSAGES = [
    "you are miles from advancement",
    "you still have a long road to travel",
    "you have barely begun",
    "hard work pays off, but not yet",
    "you have completed a portion of your advancement",
    "you have more to do, but have done much",
    "you can almost begin to see the end",
    "the road is long, but shorter than behind you",
    "you have begun the end of your work",
    "your goal draws nearer",
    "your goal is in sight",
    "your goal draws ever closer",
    "you can almost feel your ascension in your bones",
    "your ascension is nigh",
    "you are ready to ascend!",
]


def tier_progress(circle_text):
    """Cr[...] wordy message -> {"idx", "total", "pct"} or None when the
    text isn't a tier message (e.g. it's still a plain 'NN%')."""
    norm = (circle_text or "").strip().rstrip(".").lower()
    for i, msg in enumerate(TIER_MESSAGES):
        if norm == msg:
            total = len(TIER_MESSAGES)
            return {"idx": i, "total": total,
                    "pct": round(i / (total - 1) * 100)}
    return None


# --- status bar (compact Status[...] Cr[...] line) --------------------
# Shown on the connection status bar in place of the (redundant) connection
# text - same slot Changelings/Bladesingers already use. Sourced from
# self.vars, which both parse_prompt and parse_hpbar feed, so it tracks
# live even when only the MIP hpbar (no text prompt) is on screen.
def format_status_bar(v):
    """self.vars-shaped dict -> the compact 'Status[w.%|p..|v..|r.%] Cr[.]'
    bar string. Each Status[] field is included only if present, so the
    bar degrades gracefully before all data has arrived. Cr[] shows the
    tier % once Circle is capped (v["tier"] set by tier_progress) instead
    of the wordy advancement message, which doesn't fit the bar."""
    parts = []
    if "worth" in v:
        parts.append(f"w{v['worth']}%")
    if "protection" in v:
        parts.append("pON" if v["protection"] else "pOFF")
    if "veil" in v:
        parts.append("vON" if v["veil"] else "vOFF")
    if "reset" in v:
        parts.append(f"r{v['reset']}%")
    out = f"Status[{'|'.join(parts)}]" if parts else ""
    tier = v.get("tier")
    if tier:
        cr = f"{tier['pct']}%"
    else:
        cr = v.get("circle")
    if cr is not None:
        out = f"{out} Cr[{cr}]" if out else f"Cr[{cr}]"
    return out


# --- power-firing lines (decrement remaining uses between `powers`) ----
# Substring signature -> power. Decrement is an ESTIMATE between `powers`
# readouts (which resync the truth), so pick a once-per-cast line. Seeded
# from the user's drain/dream samples; extend as more powers are sampled.
POWER_FIRE = {
    "energized mist returns to you": "drain",
    "shadows of death and darkness heal your mind and body": "dream",
}


def power_fired(line):
    """Power whose fire-signature appears in this line, or None."""
    low = line.lower()
    for sig, power in POWER_FIRE.items():
        if sig in low:
            return power
    return None
