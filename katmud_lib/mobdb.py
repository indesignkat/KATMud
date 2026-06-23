"""katmud_lib.mobdb - parse a `vscan1` report into a mob record.

vscan1 (a Bladesinger power on Din) prints a boxed report per mob:

    [[                                                             ]]
    [[              A Giant Glazed Chocolate Cake Donut            ]]
    [[   Race: Donut                                               ]]
    [[   Class: 3,861,480                                          ]]
    [[   Offensive                                                 ]]
    [[   Edged:           [0]         Blunt:           [763]       ]]
    ... offensive resists ...
    [[   Attack pattern: 4                                         ]]
    [[   Penetration: 0%                                           ]]
    [[   This enemy does NOT have custom attacks.                  ]]
    [[   Specials                                                  ]]
    [[   Chance:  40%                                              ]]   (may be absent)
    [[   Damage types: Mind                                        ]]
    [[   WC:           750                                         ]]
    [[   Defensive                                                 ]]
    [[   Edged:           [527]       Blunt:           [527]       ]]
    ... defensive resists ...
    [[   Lives: 1/1                                                ]]
    [[   Dodge / Defense / Regeneration ...                        ]]
    [[   Miscellaneous                                             ]]
    [[   Hunts / Aggressive / Switches / Moves / Peaceable: Yes/No ]]

The Edged:/Blunt:/... resist rows appear under BOTH "Offensive" and
"Defensive", so parsing tracks the current section to route them. Area +
coder come from the map (the room's rating), NOT from this box - the parser
returns only the per-mob fields. Returns None for a box that isn't a mob
(e.g. a help `[[ ... ]]` box), so the caller can ignore it.
"""

import re

_BOX_RE = re.compile(r"^\s*\[\[(.*)\]\]\s*$")
_SECTIONS = {"Offensive": "off", "Defensive": "def",
             "Specials": "spec", "Miscellaneous": "misc"}
# damage-type label -> column suffix (order as displayed)
_DMG = {"edged": "edged", "blunt": "blunt", "fire": "fire", "ice": "ice",
        "acid": "acid", "electric": "electric", "mind": "mind",
        "energy": "energy", "poison": "poison", "radiation": "radiation"}
_RESIST_RE = re.compile(r"([A-Za-z]+):\s*\[(-?\d+)\]")
_BOOL = {"hunts", "aggressive", "switches", "moves", "peaceable"}


def is_box_line(line):
    return bool(_BOX_RE.match(line))


def box_inner(line):
    """Inner text of a `[[ ... ]]` line (stripped), or None."""
    m = _BOX_RE.match(line)
    return m.group(1).strip() if m else None


def _to_int(s):
    try:
        return int(s.replace(",", "").replace("%", "").strip())
    except (ValueError, AttributeError):
        return None


def parse_vscan(lines):
    """Parse one captured box (a list of raw output lines). Returns a dict
    of mob fields (always includes 'name'), or None if the box is not a
    mob report (no Race/Class signature)."""
    inner = [box_inner(ln) for ln in lines if is_box_line(ln)]
    inner = [s for s in inner if s is not None]
    data = {}
    name = None
    section = None
    for line in inner:
        if not line:
            continue
        if line in _SECTIONS:
            section = _SECTIONS[line]
            continue
        if name is None and ":" not in line and "[" not in line \
                and not line.lower().startswith("this enemy"):
            name = line                          # centered title line
            continue
        # resist rows (under Offensive / Defensive) - may carry two pairs
        if "[" in line and section in ("off", "def"):
            for label, val in _RESIST_RE.findall(line):
                suf = _DMG.get(label.lower())
                if suf:
                    data[f"{section}_{suf}"] = _to_int(val)
            continue
        # custom-attack flag (a sentence, no colon)
        low = line.lower()
        if "custom attack" in low:
            data["custom_attacks"] = 0 if "not have" in low or "no custom" \
                in low else 1
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "race":
            data["race"] = val
        elif key == "alignment":
            data["alignment"] = _to_int(val)
        elif key == "class":
            data["class"] = _to_int(val)
        elif key == "attack pattern":
            data["attack_pattern"] = val
        elif key == "penetration":
            data["penetration"] = _to_int(val)
        elif key == "chance":
            data["special_chance"] = _to_int(val)
        elif key == "damage types":
            data["special_damage"] = val
        elif key == "wc":
            data["special_wc"] = val
        elif key == "lives":
            data["lives"] = val
        elif key == "dodge":
            data["dodge"] = _to_int(val)
        elif key == "defense":
            data["defense"] = _to_int(val)
        elif key == "regeneration":
            data["regeneration"] = _to_int(val)
        elif key in _BOOL:
            data[key] = 1 if val.lower().startswith("y") else 0
    if name is None or "race" not in data:
        return None                              # not a mob box
    data["name"] = name
    return data
