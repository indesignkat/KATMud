"""verify_3k_bots.py - simulate each muds/3k/bots/*.json path against the
real 3k.map graph to check the converted route still walks cleanly.

For every Tier-1 (non-_incomplete) bot: find candidate start rooms by
matching the bot's "area" field against TinMap room.area, then replay the
path's exit commands from each candidate. A path step may be a grouped
tt++ token ("get key;n") - only sub-tokens that match a real exit at the
current room move the simulated position; the rest are treated as in-room
actions (look/get/search/etc), same as the live Run engine which just
sends them and only cares whether the *room* exit commands resolve.

A candidate "succeeds" if every step that needed to move found a matching
exit. Reports, per bot: which start room(s) make the whole route walk
clean, or the first step that breaks (with the room name + its real exits)
if none do.

Usage: python tools/verify_3k_bots.py
"""

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from katmud_lib import mapdata, paths

MUD = "3k"
BOTS_DIR = os.path.join("muds", "3k", "bots")

# Bot area labels that can't be name-matched to their real map area at all:
# "Angels" the bot hunts the archangels up the Kabbalah Tree of Life, but
# "Angels" on the map is the (unrelated) Angel guild hall - confirmed by
# walking the path manually; the doorway/onward route lives in area
# "Tree of Life" starting at room 24584.
AREA_ALIASES = {
    "angels": "Tree of Life",
}


def load_bots():
    bots = []
    for fn in sorted(glob.glob(os.path.join(BOTS_DIR, "*.json"))):
        with open(fn, encoding="utf-8") as f:
            data = json.load(f)
        bots.append((os.path.basename(fn), data))
    return bots


def area_index(tmap):
    idx = {}
    for rid, room in tmap.rooms.items():
        a = (room.area or "").strip().lower()
        if a:
            idx.setdefault(a, []).append(rid)
    return idx


def _norm(s):
    return "".join(ch for ch in s.lower() if ch.isalnum())


def find_area_candidates(idx, bot_area):
    """Bot 'area' fields are casual tt++ labels, not always the map's
    canonical area name (e.g. "Aegis All" vs "Aegis Global", "Hoteltrans"
    vs "Hotel Transylvania"). Exact match first; else fuzzy: normalized
    substring either way, or any >=4-char word of the bot area appearing
    in the canonical name. Returns (matched_area_names, room_ids)."""
    exact = idx.get(bot_area.lower())
    if exact:
        return [bot_area.lower()], exact
    nb = _norm(bot_area)
    words = [w for w in re.findall(r"[a-z0-9]+", bot_area.lower())
             if len(w) >= 5]
    hits = []
    for area in idx:
        na = _norm(area)
        # only nb-in-na: the bot's casual label is expected to be a
        # truncation/abbreviation of the canonical name, not the reverse -
        # the reverse direction matches short canonical names too eagerly
        # (e.g. an area literally named "House" inside "Treehouse Blue").
        if nb in na or any(w in na for w in words):
            hits.append(area)
    rooms = [rid for a in hits for rid in idx[a]]
    return hits, rooms


COMPASS_WORDS = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se",
    "southwest": "sw", "up": "u", "down": "d",
}


def flatten_step(step):
    """A path step may be a grouped tt++ token; split on ';' so each
    sub-command can be checked against the room's real exits separately."""
    return [s.strip() for s in step.split(";") if s.strip()]


def find_exit(room, sub):
    """Case-insensitive lookup of `sub` among room.exits, with two
    fallbacks: (1) full compass words ("west") map to their abbreviation
    ("w") and match by dirbits, since the map can capture either spelling
    depending on what the original player typed; (2) a room's recorded
    exit command can itself be a compound ("search shelves;get gloves;e")
    captured the first time a precondition (item, unlocked door) was met -
    if `sub` matches the compound's TRAILING sub-command, the bare move
    still resolves to the same destination (the precondition need only be
    met once, e.g. the item stays in inventory)."""
    low = sub.lower()
    for cmd in room.exits:
        if cmd.lower() == low:
            return cmd
    abbr = COMPASS_WORDS.get(low)
    if abbr:
        target_bits = mapdata.DIR_TO_BITS.get(abbr)
        for cmd in room.exits:
            if cmd.lower() == abbr:
                return cmd
            if target_bits and room.emeta.get(cmd, (0, 0))[0] == target_bits:
                return cmd
    for cmd in room.exits:
        if ";" in cmd:
            tail = cmd.rsplit(";", 1)[-1].strip().lower()
            if tail == low or COMPASS_WORDS.get(low) == tail:
                return cmd
    return None


COMPASS_ALL = set(COMPASS_WORDS) | set(COMPASS_WORDS.values())


def simulate(tmap, start, steps):
    """Replay steps from `start`. Returns (ok, end_room, break_info).
    break_info is None on success, else (step_idx, raw_step, sub_cmd,
    room_id, room_name, available_exits).

    An ungrouped step is always a required move (e.g. "doorway", "proceed",
    "enter portal" - special non-compass exits that only ever appear
    alone). Within a grouped tt++ token ("search shelf;get gloves;n", or
    "s;insert seed;chop tree;get all") only the compass-direction
    sub-command is required, wherever it falls in the group - the rest are
    in-room actions (get/search/unlock/nod/...) that don't move the player
    and are taken opportunistically but never fail the route."""
    cur = start
    for i, raw in enumerate(steps):
        subs = flatten_step(raw)
        for j, sub in enumerate(subs):
            required = len(subs) == 1 or sub.lower() in COMPASS_ALL
            room = tmap.rooms.get(cur)
            if room is None:
                return False, cur, (i, raw, sub, cur, "?", [])
            match = find_exit(room, sub)
            if match is None:
                if required:
                    return False, cur, (i, raw, sub, cur, room.name,
                                        list(room.exits))
                continue          # optional in-room action - no-op
            nxt = tmap.follow(cur, match)
            if nxt is None:
                return False, cur, (i, raw, sub, cur, room.name, list(room.exits))
            cur = nxt
    return True, cur, None


def main():
    map_path = paths.map_file(MUD)
    print(f"Loading {map_path} ...")
    tmap = mapdata.TinMap(map_path)
    print(f"{len(tmap.rooms)} rooms loaded"
          f"{', ' + str(len(tmap.patch_errors)) + ' patch errors' if tmap.patch_errors else ''}.\n")

    idx = area_index(tmap)
    bots = load_bots()

    for fn, data in bots:
        name = data.get("name", fn)
        area = (data.get("area") or "").strip()
        steps = data.get("path") or []
        if data.get("_incomplete"):
            print(f"{name:16} SKIP (incomplete - trigger-driven, not Tier-1)")
            continue
        if not steps:
            print(f"{name:16} SKIP (no path)")
            continue
        lookup_area = AREA_ALIASES.get(area.lower(), area)
        matched_areas, cands = find_area_candidates(idx, lookup_area)
        if not cands:
            print(f"{name:16} area '{area}': NO ROOMS TAGGED with this area "
                  f"on the map (even fuzzily) - cannot verify a start room.")
            continue
        fuzzy = matched_areas != [area.lower()]
        area_label = (area if not fuzzy else
                      f"{area}\" ~ map \"{', '.join(sorted(matched_areas))}")
        oks = []
        best_break = None
        best_break_depth = -1
        for start in cands:
            ok, end, brk = simulate(tmap, start, steps)
            if ok:
                oks.append((start, end))
            elif brk and brk[0] > best_break_depth:
                best_break_depth = brk[0]
                best_break = (start, brk)
        if oks:
            starts = ", ".join(f"#{s}->#{e}" for s, e in oks[:3])
            more = f" (+{len(oks)-3} more)" if len(oks) > 3 else ""
            print(f"{name:16} OK  - {len(steps)} steps clean from "
                  f"{len(oks)}/{len(cands)} candidate room(s) tagged "
                  f"'{area_label}': {starts}{more}")
        else:
            s, (i, raw, sub, rid, rname, exits) = best_break
            print(f"{name:16} FAIL - all {len(cands)} candidate room(s) "
                  f"tagged '{area_label}' broke; furthest got to step {i+1}/"
                  f"{len(steps)} (start #{s}). Broke on '{sub}' (from "
                  f"token '{raw}') at room #{rid} \"{rname}\" - real exits: "
                  f"{exits}")


if __name__ == "__main__":
    main()
