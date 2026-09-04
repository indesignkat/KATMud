"""katmud_lib.blade - Bladesinger (3s) guild helpers.

Bladesingers have NO guild-specific MIP tag; like necros their data comes
off plain text readouts plus the FFF composite. This module parses the
`skills` readout (see supporting docs/bladesingers.txt) and the prompt's
G2N line so the client can answer "how much more GXP do I need to raise
skill X?".

Key relationship (verified against the reference readout):
    available-to-spend  ==  next_glvl_cost - G2N  ==  the "Total GXP" line
e.g. glvl 73 costs 123,550,000 and G2N (gxp to next level) is 23,257,983,
so banked/spendable GXP = 100,292,017, which is exactly "Total GXP".

We can therefore compute spendable GXP LIVE from the streaming prompt
(G2N + G2N%) without re-reading `skills`:
    next_cost ~= G2N / (1 - G2N%/100)        # ~exact; snap to the table
    available  = exact_next_cost - G2N
GXP needed to raise a tracked skill = skill_cost - available (>=0).
"""

import re
import tkinter as tk

from . import widgets

# One full blur reset, in seconds. NOT estimated - measured off the 3s GMCP
# Guild.State feed (capture logs/gmcp_20260830_123436.log, 2026-08-30):
# `reset` climbs by exactly 2/27 % per combat round across 474 consecutive
# samples with zero variation, so 0->100% is 1350 rounds. The same feed's
# `portal_reset` (1/4 % per round, 400 rounds) wrapped twice 799.952s apart
# against 800s predicted, pinning one round at 2.000s - so the full cycle is
# 2700s, accurate to well under a second across the whole 45 minutes.
#
# All `max` charges refill at the wrap and any unspent charge is LOST, so this
# is the deadline the auto-blur kickoff counts back from. Measured on 3s; the
# `blur_reset_secs` setting still overrides it if another mud differs.
BLUR_RESET_SECS = 2700
BLUR_RESET_ROUNDS = 1350
BLUR_ROUND_SECS = 2.0

# How long ONE blur lasts. Also measured, not guessed - and measured on the
# reset counter itself rather than the clock, which is what makes it exact:
# the counter ticks once per combat round, so the % it advances between the
# cast and the fade IS the duration in rounds. Same capture, both blurs:
#
#   cast 56.5926% -> fade 60.1481%  = 3.5556% = 48.0 rounds
#   cast 76.4444% -> fade 80.0000%  = 3.5556% = 48.0 rounds
#
# (cast = the hpbar B-count dropping, fade = "The reflective shadow
# surrounding you disintegrates into nothingness.")  3.5556% is 48/1350
# exactly. Wall time was 95s and 96s, but that only matches because combat
# was near-continuous - the ROUND count is the real quantity, since a blur
# does not tick down while idle.
BLUR_DURATION_ROUNDS = 48
BLUR_DURATION_SECS = BLUR_DURATION_ROUNDS * BLUR_ROUND_SECS   # 96

# Guild-level cost table from supporting docs/bladesingers.txt. The doc
# lists glvl 42 as "1,900,000" which is a clear typo (41=17M, 43=21M);
# corrected here to 19,000,000 so the level-snap stays monotonic.
GLVL_COST = {
    1: 0, 2: 500, 3: 1_000, 4: 2_000, 5: 4_000, 6: 6_000, 7: 10_000,
    8: 15_000, 9: 22_500, 10: 35_000, 11: 50_000, 12: 75_000, 13: 100_000,
    14: 150_000, 15: 200_000, 16: 275_000, 17: 350_000, 18: 450_000,
    19: 550_000, 20: 700_000, 21: 900_000, 22: 1_250_000, 23: 1_500_000,
    24: 1_750_000, 25: 2_000_000, 26: 2_250_000, 27: 2_500_000,
    28: 2_750_000, 29: 3_000_000, 30: 3_500_000, 31: 4_000_000,
    32: 4_750_000, 33: 5_500_000, 34: 6_500_000, 35: 7_500_000,
    36: 8_750_000, 37: 10_000_000, 38: 11_500_000, 39: 13_000_000,
    40: 15_000_000, 41: 17_000_000, 42: 19_000_000, 43: 21_000_000,
    44: 23_000_000, 45: 25_000_000, 46: 27_000_000, 47: 29_000_000,
    48: 31_000_000, 49: 33_000_000, 50: 35_000_000, 51: 38_850_000,
    52: 42_700_000, 53: 46_550_000, 54: 50_400_000, 55: 54_250_000,
    56: 58_100_000, 57: 61_950_000, 58: 65_800_000, 59: 69_650_000,
    60: 73_500_000, 61: 77_350_000, 62: 81_200_000, 63: 85_050_000,
    64: 88_900_000, 65: 92_750_000, 66: 96_600_000, 67: 100_450_000,
    68: 104_300_000, 69: 108_150_000, 70: 112_000_000, 71: 115_850_000,
    72: 119_700_000, 73: 123_550_000, 74: 127_400_000, 75: 131_250_000,
    76: 135_100_000, 77: 138_950_000, 78: 142_800_000, 79: 146_650_000,
    80: 150_500_000, 81: 154_350_000, 82: 158_200_000, 83: 162_050_000,
    84: 165_900_000, 85: 169_750_000, 86: 173_600_000, 87: 177_450_000,
    88: 181_300_000, 89: 185_150_000, 90: 189_000_000, 91: 192_850_000,
    92: 196_700_000, 93: 200_550_000, 94: 204_400_000, 95: 208_250_000,
    96: 212_100_000, 97: 215_950_000, 98: 219_800_000, 99: 223_650_000,
    100: 227_500_000,
}

# Box row: "<name>  <rank>  <cost-or-N/A>" inside the | ... | borders.
_SKILL_RE = re.compile(r"^\s*(.+?)\s{2,}(\S.*?)\s{2,}([\d,]+|N/A)\s*$")
_TOTAL_GXP_RE = re.compile(r"Total GXP:\s*([\d,]+)")
_TOTAL_SPENT_RE = re.compile(r"Total Spent:\s*([\d,]+)")
# Brackets the `skills` readout, so a skill that vanishes cannot go stale.
SKILLS_HEADER = "The Skills of"
SKILLS_FOOTER = "Total Spent:"
# Prompt line: "G2N:23,257,983 G2N%: 81.1752 L:0 WMast:40.6184"
_G2N_RE = re.compile(r"G2N:\s*([\d,]+)\s+G2N%:\s*([\d.]+)")


def _box_inner(line):
    """Return the text between the first and last '|' of a box line, or
    the whole line if it has no border."""
    if "|" in line:
        i, j = line.find("|"), line.rfind("|")
        if j > i:
            return line[i + 1:j]
    return line


def parse_skill_row(line):
    """A skill row -> {name, rank, cost, depth}, or None.

    `cost` is None for 'N/A' (the skill is maxed). `depth` comes from the
    indentation INSIDE the box borders, which is what carries the tree:
    2 spaces is a category, 4 a skill, 6 a sub-skill ('Elven Smithing'
    under 'Item Preparation', the Portal trio under 'Portal Magic').
    """
    inner = _box_inner(line)
    m = _SKILL_RE.match(inner)
    if not m:
        return None
    name = m.group(1).strip().lower()
    if not name or "total" in name:
        return None
    cost_s = m.group(3)
    indent = len(inner) - len(inner.lstrip(" "))
    return {"name": name,
            "rank": m.group(2).strip(),
            "cost": None if cost_s == "N/A" else int(cost_s.replace(",", "")),
            "depth": max(0, (indent - 2) // 2)}


def parse_skill_line(line):
    """A skill row -> (name_lower, cost_or_None). 'N/A' (maxed) -> None
    cost. Returns None for non-skill lines. Kept as the narrow view
    blade_scan_line already uses; parse_skill_row is the full one."""
    row = parse_skill_row(line)
    return None if row is None else (row["name"], row["cost"])


def parse_total_gxp(line):
    m = _TOTAL_GXP_RE.search(line)
    return int(m.group(1).replace(",", "")) if m else None


# --- the prompt's active-effects token --------------------------------
# Line 2 of the 3s bladesinger prompt:
#   [Focused] [CoCsBlMsRv] C:3/15 E: 90%
# ONE bracketed token holding the two-letter code of every ACTIVE effect,
# concatenated. A code that is absent is NOT active - that is the whole
# signal. GAME FACT (user, 2026-09-03): the ORDER VARIES, and there are
# exactly SIX codes - Co/Cs/Ms, plus Bl while a blur is running, Rv for
# revalrie out of combat, and Fl for Faerielight.
#
# The token is still recognised by its SHAPE rather than by matching the
# six: an even number of characters splitting cleanly into Capital+
# lowercase pairs. That rejects "[Focused]" (odd length, and "cu" is not
# Capital+lowercase) without a special case, and it fails SAFE - an
# unrecognised code is carried through unrendered instead of making the
# whole token unparseable, which would read as "everything is down" and
# paint the bar red on a false alarm.
FLAG_NAMES = {"Co": "cover", "Cs": "shadows", "Ms": "mshield",
              "Bl": "blur", "Rv": "revalrie", "Fl": "faerielight"}
# The three the panel's segmented bar shows, in display order.
FLAG_BAR = ("Co", "Cs", "Ms")
_FLAG_TOKEN_RE = re.compile(r"\[((?:[A-Z][a-z])+)\]")


def parse_flags(line):
    """Prompt line -> {effect_name: True} for every ACTIVE effect, or None
    if the line carries no effects token.

    None and {} mean different things: None is "this line does not say"
    (leave the last known state alone), {} is "the token was there and it
    was empty" (everything is down). Getting that wrong would show green
    on every line that is not the prompt.
    """
    m = _FLAG_TOKEN_RE.search(line or "")
    if not m:
        # An empty token still means "nothing active", so look for it too.
        return {} if re.search(r"\[\]", line or "") else None
    body = m.group(1)
    out = {}
    for i in range(0, len(body), 2):
        code = body[i:i + 2]
        out[FLAG_NAMES.get(code, code.lower())] = True
    return out


def parse_total_spent(line):
    m = _TOTAL_SPENT_RE.search(line)
    return int(m.group(1).replace(",", "")) if m else None


def parse_g2n(line):
    """Prompt G2N line -> (g2n:int, g2n_pct:float) or None."""
    m = _G2N_RE.search(line)
    if not m:
        return None
    return int(m.group(1).replace(",", "")), float(m.group(2))


def glvl_from_g2n(g2n, g2n_pct):
    """Derive (current_glvl, next_glvl_cost) by snapping the implied next
    cost to the cost table. Returns None if g2n_pct is degenerate."""
    if g2n_pct >= 100 or g2n_pct < 0:
        return None
    approx_next = g2n / (1 - g2n_pct / 100.0)
    nxt = min(GLVL_COST, key=lambda lv: abs(GLVL_COST[lv] - approx_next))
    return nxt - 1, GLVL_COST[nxt]


def available_gxp(g2n, g2n_pct):
    """Spendable GXP = exact next-level cost - G2N (exact). None if it
    can't be derived."""
    got = glvl_from_g2n(g2n, g2n_pct)
    if got is None:
        return None
    _, next_cost = got
    return max(0, next_cost - g2n)


def fmt_gxp(n):
    """123550000 -> '123.55M', 30000000 -> '30M', None -> '?'."""
    if n is None:
        return "?"
    neg = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000:
        return f"{neg}{n / 1_000_000:.2f}M".replace(".00M", "M")
    if n >= 1_000:
        return f"{neg}{n / 1_000:.0f}K"
    return f"{neg}{n}"


def fmt_secs(s):
    """Seconds -> compact '45m', '1h05m', '40s'."""
    if s is None:
        return "?"
    s = int(s)
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m" if sec < 10 else f"{m}m{sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


# --- the detached Bladesinger panel -----------------------------------
# Same ground as the Elemental panel, so the two windows match.
BLADE_BG = "#0c0c12"
# A GXP/skills PLANNER, deliberately not a combat readout: the pools, blur
# and portal timers already live in the status bar and the info pane, and
# duplicating them would just be a second place to keep correct.
#
# The property that makes it worth a window is that affordability tracks
# LIVE off a readout you type once. `skills` supplies the tree, the ranks
# and the costs; `available` is derived from the STREAMING G2N prompt (see
# available_gxp above), so the READY / need column keeps moving as GXP comes
# in without re-reading anything.

def skill_lines(rows, available):
    """Skill rows + spendable GXP -> [(text, tag)] for the panel.

    Tags: "sec" for a top-level category, "ok" for a skill you can afford
    right now, "dim" for one that is maxed, "val" otherwise. Rows keep the
    readout's own order, because that order IS the tree.
    """
    out = []
    for r in rows or ():
        name = "  " * r["depth"] + r["name"].title()
        if r["cost"] is None:
            # A maxed CATEGORY is still a heading - depth wins over "maxed"
            # for the tag, or the tree loses its section breaks.
            out.append(("%-34s %-14s %s" % (name, r["rank"], "maxed"),
                        "sec" if r["depth"] == 0 else "dim"))
            continue
        cost = "{:,}".format(r["cost"])
        if available is None:
            status, tag = "", "val"
        elif r["cost"] <= available:
            status, tag = "READY", "ok"
        else:
            status, tag = "need " + fmt_gxp(r["cost"] - available), "val"
        out.append(("%-34s %-14s %13s  %s"
                    % (name, r["rank"], cost, status),
                    "sec" if r["depth"] == 0 else tag))
    return out


class BladeStatus(tk.Toplevel):
    """Detached 'Bladesinger Status' panel - the skills/GXP planner.

    One pane, same reasoning as ElementalStatus: this is a screen of
    numbers, not a guild with a fleet and a map to separate into tabs.

    Fed from two places:
      * the typed `skills` readout - the tree, ranks and costs. Scraped
        passively when the player types it, never auto-sent, matching how
        the elemental panel treats `guild score`.
      * the streaming G2N prompt - spendable GXP, which is what makes the
        READY column live between readouts.

    update_state() is idempotent; the client may call it on every packet.
    """

    def __init__(self, master, fonts=None, on_close=None, geometry=None,
                 topmost=False):
        super().__init__(master)
        self.title("Bladesinger Status")
        self.configure(bg=BLADE_BG)
        self.geometry(geometry or "620x560")
        self.on_close = on_close
        self.protocol("WM_DELETE_WINDOW", self._closed)
        widgets.add_window_menu(self, topmost)
        f = fonts or {}
        self.mono = f.get("mono", ("Consolas", 11))
        sb = tk.Scrollbar(self)
        sb.pack(side="right", fill="y")
        self.txt = tk.Text(self, bg=BLADE_BG, fg="#cccccc", font=self.mono,
                           wrap="none", state="disabled", padx=10, pady=8,
                           highlightthickness=0, borderwidth=0,
                           yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.configure(command=self.txt.yview)
        for tag, colour in (("val", "#cccccc"), ("dim", "#777777"),
                            ("num", "#dddddd"), ("cyan", "#6fb6d6"),
                            ("ok", "#46c246")):
            self.txt.tag_configure(tag, foreground=colour)
        self.txt.tag_configure("sec", foreground="#d79030",
                               font=f.get("mono_bold",
                                          ("Consolas", 11, "bold")))

    def _closed(self):
        if self.on_close:
            self.on_close()
        self.destroy()

    def update_state(self, rows, available, glvl, total_spent):
        if not self.winfo_exists():
            return
        pos = self.txt.yview()[0]
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("end", "GXP\n", "sec")
        self.txt.insert("end", "  spendable   %s\n"
                        % ("{:,}".format(available) if available is not None
                           else "? (no G2N prompt yet)"),
                        "ok" if available else "dim")
        if glvl is not None:
            self.txt.insert("end", "  guild level %d\n" % glvl, "cyan")
        if total_spent is not None:
            self.txt.insert("end", "  total spent %s\n"
                            % "{:,}".format(total_spent), "dim")
        self.txt.insert("end", "\nSkills\n", "sec")
        lines = skill_lines(rows, available)
        if not lines:
            self.txt.insert("end", "  none yet - type `skills`\n", "dim")
        for text, tag in lines:
            self.txt.insert("end", "  " + text + "\n", tag)
        self.txt.configure(state="disabled")
        self.txt.yview_moveto(pos)
