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


def parse_skill_line(line):
    """A skill row -> (name_lower, cost_or_None). 'N/A' (maxed) -> None
    cost. Returns None for non-skill lines."""
    inner = _box_inner(line)
    m = _SKILL_RE.match(inner)
    if not m:
        return None
    name = m.group(1).strip().lower()
    if not name or "total" in name:
        return None
    cost_s = m.group(3)
    cost = None if cost_s == "N/A" else int(cost_s.replace(",", ""))
    return name, cost


def parse_total_gxp(line):
    m = _TOTAL_GXP_RE.search(line)
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
