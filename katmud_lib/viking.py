"""katmud_lib.viking - the Vikings guild status window and its MIP
decoders.

GUILD DISCIPLINE (spec 2.4): everything Viking-specific lives here and is
wired in only through muds/3s/guilds/vikings.json (the BBE tag -> the
mip_viking handler on the client). Nothing here runs for other guilds.

WIRE FORMAT. The MUD sends ALL Viking live feeds (vtoggle mip_extra,
mip_city, mip_trade_goods, mip_map, mip_voyage) under a SINGLE MIP tag:
BBE. The payload is KEY^^VALUE^^KEY^^VALUE... - double-caret, because the
base MIP layer already owns '~'. Empty values occur ('CARTS^^^^CIDLE' ->
CARTS=''). Long feeds (esp. mip_city) arrive CHUNKED across several
consecutive BBE packets, each split on a key boundary, so the client
MERGES every packet's keys into one running state dict (parse_bbe gives
just the keys present in one packet; never assume a packet is complete).
Since the 2026-07 trade expansion even TGOODS arrives in pieces - the
client merges its chunks by hold id (see merge_tgoods).
"""

import itertools
import math
import re
import tkinter as tk


# ======================================================================
# Decoders
# ======================================================================
def parse_bbe(data):
    """One BBE packet -> {KEY: VALUE} for the keys present in it.

    Pairs are both separated and delimited by '^^'. Even tokens are keys,
    odd tokens their values; a dangling final token (or the occasional
    stray '^' the MUD emits inside the city feed) is ignored rather than
    corrupting alignment of the keys we do care about."""
    toks = data.split("^^")
    out = {}
    for i in range(0, len(toks) - 1, 2):
        # The city feed occasionally emits one stray '^' (e.g.
        # 'VARANG^^^^^THRALLS'); it leaves key/value alignment intact but
        # prepends '^' to the next key, so strip it.
        key = toks[i].strip().lstrip("^")
        if key:
            out[key] = toks[i + 1]
    return out


# TGOODS good-letter -> name, and hold-id -> display name. Both orderings
# are fixed by the MUD (confirmed against the STANDINGS/VREP keys and the
# reference client's Goods screenshot); ids 11-13 keep the same style.
# The 2026-07-09 trade expansion added ore/salted_fish/bread/fine_furs/
# tools/gemstones; their letters follow the TGOODS packet order, which
# matches the `vtrade prices` table order (t,o,i,f,h,g,m,a,r,s,k,b,e,l,j).
# Multi-word goods use the mission board's underscore form as the
# canonical name ('salted_fish'), matching the vtrade parsers below.
GOOD_NAMES = {
    "f": "furs", "h": "fish", "m": "mead",
    "r": "runestones", "s": "spoils", "t": "timber", "i": "iron",
    "g": "grain", "o": "ore", "k": "salted_fish", "b": "bread",
    "e": "fine_furs", "l": "tools", "j": "gemstones",
}
# Goods added in the 2026-07-17 trade expansion, seen in `vtrade prices`
# and on the mission board. Their TGOODS letters haven't been captured
# yet, so they live here rather than in GOOD_NAMES.
EXTRA_GOODS = ("sunstone", "honey", "weapons", "armour", "finery")
# Hold id -> CITY name (user preference 2026-07-11: city names, not
# lineage 'X Hold' names - e.g. lineage Eiriksson's city is Eiriksby).
HOLD_NAMES = {
    0: "Midgard", 1: "Lodbrok", 2: "Eiriksby",
    3: "Imair", 4: "Holmgard", 5: "Hafrfjord",
    6: "Uppsala", 7: "Borgarfjord", 8: "Vestergotland",
    9: "Sverkersby", 10: "Ericsgard", 11: "Birka",
    12: "Lejre", 13: "Nidaros",
}


def parse_tgoods(value):
    """TGOODS payload ->
        [(hold_id, [(good, level, supply, demand, buy, sell), ...]), ...]
    in the MUD's order (which the reference UI preserves left-to-right,
    top-to-bottom). 'level' is the demand pressure: +3..-3, where + means
    high demand / good to sell and - means oversupply. Neutral (0) goods
    are included by the MUD (since the 2026-07 doc) so every price can
    be shown. All parsing is defensive: a malformed hold or good entry
    is skipped, never raised.

    The 2026-07-09 trade expansion appended two fields per good - the
    hold's local buy/sell prices (e.g. 'k:1:0:170:29:30': buy is what
    the town charges you, sell is what it pays you, both already
    adjusted for the Trading Post tier). buy/sell are None on the old
    4-field form."""
    holds = []
    for chunk in value.split("|"):
        if "=" not in chunk:
            continue
        hid_s, entries = chunk.split("=", 1)
        try:
            hid = int(hid_s)
        except ValueError:
            continue
        goods = []
        for entry in entries.split(";"):
            parts = entry.split(":")
            if len(parts) not in (4, 6):
                continue
            letter, lvl_s, sup_s, dem_s = parts[:4]
            try:
                buy = int(parts[4]) if len(parts) == 6 else None
                sell = int(parts[5]) if len(parts) == 6 else None
                goods.append((GOOD_NAMES.get(letter, letter),
                              int(lvl_s), int(sup_s), int(dem_s),
                              buy, sell))
            except ValueError:
                continue
        holds.append((hid, goods))
    return holds


def merge_tgoods(old, new):
    """Merge a TGOODS chunk into the previous TGOODS value by hold id.

    The 2026-07-09 trade expansion (15 goods x 6 fields x 14 holds) grew
    the payload past one packet, so TGOODS now arrives in pieces and the
    old last-chunk-wins state merge left only the tail holds (seen live
    2026-07-11: only Skjoldung/Sigurdsson rendered on the Goods tab).
    Holds in `new` replace same-id holds in `old`; output is ordered by
    hold id (= the MUD's 0-13 order). Assumes chunks split on hold
    boundaries - if holds still go missing live, get a #mipraw capture
    to check for mid-hold splits."""
    merged = {}
    for value in (old, new):
        for c in value.split("|"):
            if "=" not in c:
                continue
            hid = c.split("=", 1)[0].strip()
            if hid.isdigit():
                merged[int(hid)] = c
    return "|".join(merged[h] for h in sorted(merged))


def merge_vmapl(old, new):
    """Merge a VMAPL chunk into the previous VMAPL value by POI identity.

    The full POI list (capital + 13 lineage cities + ~40 player villages
    + seer/blot/ruins/farm landmarks + mentors) spans SIX packets (wire
    capture 2026-07-17: mip_20260717_082858.log), so the old
    last-chunk-wins merge kept only the tail - a lone mentor_jarl, which
    broke `Go <city>` and left the Map tab POI list nearly empty.
    Entries merge by (type, name). A chunk carrying the capital entry
    starts a fresh push (both captured pushes lead with it), dropping
    POIs that no longer exist - player villages come and go. Order is
    preserved (capital/lineage first, the MUD's push order)."""
    new_entries = [(e.split("|"), e) for e in new.split(";") if e]
    if any(f[0] == "capital" for f, _e in new_entries if len(f) >= 4):
        old = ""
    merged = {}
    for f, entry in [(e.split("|"), e) for e in old.split(";") if e] \
            + new_entries:
        if len(f) >= 4:
            merged[(f[0], f[1])] = entry
    return ";".join(merged.values())


_VMAPL_FRAG_RE = re.compile(r"^VMAPL_(\d+)of(\d+)$")


def reassemble_vmapl(state, upd):
    """Undo the MUD's sub-chunking of individual VMAPL chunks (wire
    capture 2026-07-30: mip_20260730_130257.log). Each VMAPL chunk that
    merge_vmapl expects can now itself arrive split across keys named
    VMAPL_<n>of<m> instead of one plain VMAPL key - concatenating the
    pieces in order reconstructs the original chunk text byte-for-byte
    (splits land mid-word, e.g. 'Hafr' + 'fjord'). Buffers partial bursts
    in state['_vmapl_frag'] across calls; once the final piece (n==m)
    arrives, replaces the fragment keys in `upd` with a plain 'VMAPL' key
    so the existing merge_vmapl path handles it unchanged."""
    for k in [k for k in upd if _VMAPL_FRAG_RE.match(k)]:
        n, m = (int(g) for g in _VMAPL_FRAG_RE.match(k).groups())
        val = upd.pop(k)
        state["_vmapl_frag"] = val if n == 1 else \
            state.get("_vmapl_frag", "") + val
        if n == m:
            upd["VMAPL"] = state.pop("_vmapl_frag", "")


# --- mip_city decoders (City tab) -------------------------------------
# These cover the keys the reference City tab shows. Validated against the
# 02:01 capture for the populated keys (DALER, GOD_POWER*, SHIPS, WSTOCK,
# BUILDINGS, MONUMENTS); CARTS/MARKET/INCOMING were empty in that capture,
# so their populated rendering is best-effort per the guild help doc and
# flagged for validation once carts are dispatched.
def split_entries(value):
    """Semicolon list of pipe-records -> [[field, ...], ...], skipping
    empty entries (an empty feed value yields [])."""
    out = []
    for entry in value.split(";"):
        if entry:
            out.append(entry.split("|"))
    return out


def field(rec, i, default="?"):
    """Safe positional access into a parsed pipe-record."""
    return rec[i] if 0 <= i < len(rec) else default


def parse_wstock(value):
    """WSTOCK 'good|amount|freshness[|grade];...' ->
    {good: [(amount, fresh%, grade), ...]}. A good can hold several
    batches at different freshness. Per the 2026-07 guild help doc,
    perishables and graded goods (mead, refined goods, iron) append a
    4th grade-label field; durable goods have none (grade '')."""
    out = {}
    for f in split_entries(value):
        if len(f) not in (3, 4):
            continue
        good, amt, fresh = f[0], f[1], f[2]
        grade = f[3] if len(f) == 4 else ""
        try:
            batch = (int(amt), int(fresh), grade)
        except ValueError:
            continue
        out.setdefault(good, []).append(batch)
    return out


def parse_buildings(value):
    """BUILDINGS 'name:tier,name:tier,...' -> [(name, tier), ...]."""
    out = []
    for pair in value.split(","):
        if ":" not in pair:
            continue
        name, tier = pair.rsplit(":", 1)
        try:
            out.append((name, int(tier)))
        except ValueError:
            continue
    return out


def parse_build_res(value):
    """'iron:9/9,timber:14/14' -> [(good, have, need), ...]."""
    out = []
    for part in value.split(","):
        if ":" not in part or "/" not in part:
            continue
        good, hn = part.split(":", 1)
        have, need = hn.split("/", 1)
        try:
            out.append((good, int(have), int(need)))
        except ValueError:
            continue
    return out


def parse_construction(value):
    """BUILDS 'name|tier|a|b|secs_remaining|secs_total|resources' -> dict
    for the building currently under construction, or None. (Fields a/b -
    both 23 in samples - are unidentified and not shown.)"""
    f = value.split("|")
    if len(f) < 6 or not f[0]:
        return None
    try:
        return {"name": f[0], "tier": f[1], "remaining": int(f[4]),
                "total": int(f[5]),
                "resources": parse_build_res(f[6]) if len(f) > 6 else []}
    except ValueError:
        return None


BUILD_KEYS = ("BUILDS", "BUILDINGS", "MONUMENTS", "SPROJ")


# Warehouse capacity per tier (GAME FACT from the user, 2026-07-11).
WAREHOUSE_CAPS = {1: 400, 2: 1000, 3: 1750, 4: 3000, 5: 5250}


def warehouse_cap(state):
    """Warehouse capacity from the tier table. Returns None if unknown."""
    for name, tier in parse_buildings(state.get("BUILDINGS", "")):
        if name == "warehouse":
            return WAREHOUSE_CAPS.get(tier)
    return None


def stock_totals(state):
    """Live WSTOCK feed -> {good: total units}, collapsing freshness
    batches (Missionlist doesn't care which batch a unit comes from). A
    good at zero stock is absent from WSTOCK entirely - the MUD sends
    'good|' with no amount/freshness - so callers should treat a missing
    key as zero rather than as 'unknown'."""
    totals = {}
    for good, batches in parse_wstock(state.get("WSTOCK", "")).items():
        totals[good] = sum(amt for amt, _fresh, _grade in batches)
    return totals


# THRALLS field order (GAME FACT from the user, 2026-07-11): counts of
# thralls assigned per building, zipped positionally.
THRALL_BUILDINGS = ("Thrall Pen", "Longhouse", "Warehouse", "Farm",
                    "Brewery", "Tannery", "Fishery", "Lumber Yard",
                    "Mine", "Smithy", "Watchtower", "Palisade",
                    "Salting House", "Bakehouse", "Furrier's Lodge",
                    "Smelter")


def parse_thralls(value):
    """THRALLS 'n|n|...' -> [(building, count), ...] in
    THRALL_BUILDINGS order."""
    out = []
    for name, tok in zip(THRALL_BUILDINGS, value.split("|")):
        try:
            out.append((name, int(tok)))
        except ValueError:
            continue
    return out


def parse_thrall_follower(value):
    """THRALL_FOLLOWER 'lvl|name|xp|next_lvl_xp|carried|capacity|status'
    -> dict, or None when empty (GAME FACT from the user, 2026-07-11:
    e.g. '16|Zed|10170|107216|0|6|following' = the personal thrall Zed,
    level 16, 10,170/107,216 xp, carrying 0 of 6 items, following)."""
    f = value.split("|")
    if len(f) < 2 or not f[1]:
        return None
    return {"level": f[0], "name": f[1], "xp": field(f, 2, ""),
            "next_xp": field(f, 3, ""), "carried": field(f, 4, ""),
            "cap": field(f, 5, ""), "status": field(f, 6, "")}


def parse_cellar(value):
    """CELLAR 'stock|cap|tier;qty|pct;qty|pct;...' -> {stock, cap, tier,
    brackets: [(qty, pct), ...]}, or None when empty / no mead cellar
    built (the MUD sends '0|0|0'). Doc-driven (2026-07), unvalidated
    against a live capture."""
    ents = split_entries(value)
    if not ents:
        return None
    try:
        stock, cap, tier = (int(field(ents[0], i, "0")) for i in range(3))
    except ValueError:
        return None
    if not (stock or cap or tier):
        return None
    brackets = []
    for f in ents[1:]:
        if len(f) < 2:
            continue
        try:
            brackets.append((int(f[0]), int(f[1])))
        except ValueError:
            continue
    return {"stock": stock, "cap": cap, "tier": tier,
            "brackets": brackets}


def parse_refinery(value):
    """REFINERY 'bldg:tier:stock:cap:grade,qty,pct;grade,qty,pct|...' ->
    [{bldg, tier, stock, cap, grades: [(grade, qty, pct), ...]}, ...].
    One pipe-separated entry per built refinery (Smelter/Smithy/Salting
    House/Bakehouse/Furriers Lodge); grades run low to high (curing
    grades). Doc-driven (2026-07), unvalidated against a live capture."""
    out = []
    for entry in value.split("|"):
        parts = entry.split(":", 4)
        if len(parts) < 4 or not parts[0]:
            continue
        try:
            rec = {"bldg": parts[0], "tier": int(parts[1]),
                   "stock": int(parts[2]), "cap": int(parts[3]),
                   "grades": []}
        except ValueError:
            continue
        if len(parts) > 4:
            for g in parts[4].split(";"):
                gf = g.split(",")
                if len(gf) != 3:
                    continue
                try:
                    rec["grades"].append((gf[0], int(gf[1]), int(gf[2])))
                except ValueError:
                    continue
        out.append(rec)
    return out


# Good names in the vtrade readouts can be multi-word ('Salted Fish',
# 'Fine Furs'); spaces normalize to the underscore form via _canon_good.
# Grade sub-lines under a 'units total' stock line ('56 units seax-grade')
# start with the number, not a name, so they can't spuriously match.
_VTRADE_STOCK_RE = re.compile(
    r"^-~\*\s+([A-Za-z]+(?: [A-Za-z]+)*)\s+([\d,]+)\s+units\b",
    re.IGNORECASE)
_GOOD_SET = set(GOOD_NAMES.values()) | set(EXTRA_GOODS)


def _canon_good(name):
    """A displayed good name -> the canonical underscore form used
    everywhere else ('Salted Fish' -> 'salted_fish')."""
    return name.lower().replace(" ", "_")


def parse_vtrade_stock(lines):
    """A captured `vtrade stock` readout -> {good: total units}. Used by
    Missionlist instead of the live WSTOCK/mip_trade_goods feed, which has
    been observed out of sync with the actual warehouse (e.g. showing
    runestones as empty while `vtrade stock` reports 16 units on hand).
    Each good appears on one bordered line as either '<Name>  <n> units'
    (single freshness batch) or '<Name>  <n> units total' (multiple
    batches, broken down on indented sub-lines below with no leading good
    name - those don't match this regex and are skipped, since the total
    line already has what's needed)."""
    totals = {}
    for line in lines:
        m = _VTRADE_STOCK_RE.match(line)
        if not m:
            continue
        good = _canon_good(m.group(1))
        if good not in _GOOD_SET:
            continue
        try:
            totals[good] = int(m.group(2).replace(",", ""))
        except ValueError:
            continue
    return totals


VTRADE_PRICE_RE = re.compile(
    r"^-~\*\s+([A-Za-z]+(?: [A-Za-z]+)*)\s+([\d,]+)\s+daler\b",
    re.IGNORECASE)


def parse_vtrade_prices(lines):
    """A captured `vtrade prices` readout -> {good: daler per unit}. Each
    good appears on one bordered line as '<Name>  <n> daler  [ min - max]';
    the header/border lines don't match the regex and are skipped."""
    prices = {}
    for line in lines:
        m = VTRADE_PRICE_RE.match(line)
        if not m:
            continue
        good = _canon_good(m.group(1))
        if good not in _GOOD_SET:
            continue
        try:
            prices[good] = int(m.group(2).replace(",", ""))
        except ValueError:
            continue
    return prices


def goods_value(goods, prices):
    """[(qty, good), ...] -> total daler value at current market prices
    (see parse_vtrade_prices). A good missing from `prices` counts 0."""
    return sum(q * prices.get(g, 0) for q, g in goods)


# --- `vmission list` board capture + pick optimizer (Missionlist) -----
# Unlike the live MIP feeds above, the global mission board only exists as
# the text readout of the `vmission list` command (see client.cmd_
# missionlist / _missionlist_scan_line, which captures it the same way
# cmd_vskills captures `vskills`). Each entry is wrapped across several
# decorative '-~*...*~-' bordered lines, e.g.:
#   -~*   [7045] Borgarfjord (Skallagrim: Egil's Steward needs    *~-
#   -~*                                  13 iron for              *~-
#   -~*                                  Borgarfjord.             *~-
#   -~*         Reward: 15 rep + 239 daler                        *~-
# Phrasing of the requirement varies a lot ("needs X", "bring X and Y",
# "deliver X to Z", "requires X plus Y", "X needed at Z") - rather than
# parse the prose, just regex out every '<n> <goodname>' pair and the
# Reward:/Quota: lines; the border characters ('-', '~', '*') can't
# spuriously match either pattern, so this works directly on the joined
# raw lines without stripping decoration first.
# The 2026-07 board pads the id inside the brackets ('[27137 ]').
_MISSION_ID_RE = re.compile(r"\[\s*(\d{3,6})\s*\]")
# Longest-first so overlapping names ('fine_furs' vs 'furs', 'salted_fish'
# vs 'fish') always match whole. The board displays multi-word goods with
# spaces ('Fine Furs') while the canonical form uses underscores, so each
# underscore matches either; _canon_good folds matches back to canonical.
_GOOD_ALT = "|".join(g.replace("_", "[ _]")
                     for g in sorted(_GOOD_SET, key=len, reverse=True))
_MISSION_GOOD_RE = re.compile(
    r"(\d+)\s+(" + _GOOD_ALT + r")\b", re.IGNORECASE)
# Matches the 'Need: stock/required good' line added in newer vmission output.
# Format: 'Need: 52/13 grain, 8/5 timber' -> captures (13, 'grain'), (5, 'timber').
# Prefer this over prose-matching when present to avoid double-counting goods
# that also appear in the description text.
_MISSION_NEED_RE = re.compile(
    r"\d+/(\d+)\s+(" + _GOOD_ALT + r")\b", re.IGNORECASE)
_MISSION_REWARD_RE = re.compile(
    r"Reward:\s*(\d+)\s*rep\s*\+\s*([\d,]+)\s*daler", re.IGNORECASE)
_MISSION_TOWN_RE = re.compile(r"\[\s*\d+\s*\]\s*([A-Za-z'\s]+?)\s*[:(]")
_MISSION_QUOTA_RE = re.compile(r"Quota:\s*(\d+)\s*/\s*(\d+)")


def parse_mission_board(lines):
    """A captured `vmission list` readout -> ([{id, town, requirements:
    [(qty, good), ...], rep, daler}, ...], quota_used, quota_max). A
    bracketed id with no Reward: line or no goods found (the usage/footer
    lines after the last real entry) is skipped rather than raised."""
    text = " ".join(lines)
    ids = [(m.start(), int(m.group(1)))
           for m in _MISSION_ID_RE.finditer(text)]
    missions = []
    for i, (pos, mid) in enumerate(ids):
        end = ids[i + 1][0] if i + 1 < len(ids) else len(text)
        chunk = text[pos:end]
        reqs = [(int(q), _canon_good(g))
                for q, g in _MISSION_NEED_RE.findall(chunk)]
        if not reqs:
            reqs = [(int(q), _canon_good(g))
                    for q, g in _MISSION_GOOD_RE.findall(chunk)]
        rm = _MISSION_REWARD_RE.search(chunk)
        if not rm or not reqs:
            continue
        tm = _MISSION_TOWN_RE.search(chunk)
        missions.append({
            "id": mid,
            "town": tm.group(1).strip() if tm else f"Mission {mid}",
            "requirements": reqs,
            "rep": int(rm.group(1)),
            "daler": int(rm.group(2).replace(",", "")),
        })
    qm = _MISSION_QUOTA_RE.search(text)
    used, maxq = (int(qm.group(1)), int(qm.group(2))) if qm else (0, 0)
    return missions, used, maxq


def _mission_net(mission):
    """The optimization value of one mission: its 'net' daler (reward
    minus the market value of the goods delivered, annotated by the
    caller from a `vtrade prices` capture), falling back to the raw
    reward when no prices were captured."""
    return mission.get("net", mission["daler"])


def _greedy_mission_picks(missions, stock, max_picks):
    """Approximate fallback for best_mission_picks: only used when the
    candidate set is too large to brute-force. Highest-net-first, skip
    whatever doesn't fit remaining stock; stop once net goes non-positive
    (a losing mission never helps the total)."""
    remaining = dict(stock)
    chosen = []
    for m in sorted(missions, key=_mission_net, reverse=True):
        if len(chosen) >= max_picks or _mission_net(m) <= 0:
            break
        if all(remaining.get(g, 0) >= q for q, g in m["requirements"]):
            for q, g in m["requirements"]:
                remaining[g] -= q
            chosen.append(m)
    return (chosen, sum(_mission_net(m) for m in chosen),
            sum(m["rep"] for m in chosen))


def best_mission_picks(missions, stock, max_picks):
    """Exact search over every combination of up to `max_picks` missions
    (the remaining daily quota) for the one maximizing total net daler
    (see _mission_net - reward minus market value of the delivered goods,
    so a mission that pays less than its goods are worth is never picked)
    without exceeding `stock` (a {good: available units} dict) for any
    single good - missions competing for the same good (e.g. two grain
    deliveries) can't both be picked if stock can't cover both. Ties
    broken by total rep. The mission board and quota are small enough in
    practice (~20-30 entries, quota in the single digits) that full
    enumeration is cheap; falls back to a greedy approximation if the
    candidate set ever grows large enough that it wouldn't be."""
    max_picks = max(0, min(max_picks, len(missions)))
    if max_picks == 0 or not missions:
        return [], 0, 0
    total_combos = sum(math.comb(len(missions), k)
                        for k in range(max_picks + 1))
    if total_combos > 300_000:
        return _greedy_mission_picks(missions, stock, max_picks)
    best_combo, best_net, best_rep = [], 0, 0
    for k in range(1, max_picks + 1):
        for combo in itertools.combinations(missions, k):
            used = {}
            ok = True
            for m in combo:
                for qty, good in m["requirements"]:
                    used[good] = used.get(good, 0) + qty
                    if used[good] > stock.get(good, 0):
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                continue
            net = sum(_mission_net(m) for m in combo)
            rep = sum(m["rep"] for m in combo)
            if net > best_net or (net == best_net and rep > best_rep):
                best_combo, best_net, best_rep = list(combo), net, rep
    return best_combo, best_net, best_rep


# --- `vmission newbie` board capture + pick optimizer (VNlist) --------
# Same text-readout situation as the global mission board above, but a
# different layout (see supporting docs/VN.txt for a full capture):
#   -~*   [7270] Lodbrok's Hold: Collect a sealed letter in       *~-
#   -~*        Nidaros and return to Lodbrok's Hold.              *~-
#   -~*        Fetch from Nidaros  ->  +173 daler                 *~-
#   -~*        +1 timber                                          *~-
#   -~*        +1-2 reputation                                    *~-
# (sometimes condensed onto the 'Fetch from' line itself, e.g. '+229
# daler +3 fish +1-2 rep'). Newbie errands carry no resource cost - they
# cost nothing to accept, just a daily quota slot - so there's no
# stock to conflict over; best_newbie_picks is a plain top-N sort rather
# than best_mission_picks' combinatorial search. A town temporarily out
# of errands shows as a bracket-less '<Town>: Incoming errand, available
# in Nm' filler line, which (like the footer/help lines) has no '+N
# daler' to match and is skipped.
_NEWBIE_DALER_RE = re.compile(r"\+([\d,]+)\s*daler", re.IGNORECASE)
_NEWBIE_REP_RE = re.compile(r"\+(\d+)-(\d+)\s*reputation", re.IGNORECASE)
_NEWBIE_FETCH_RE = re.compile(r"Fetch from\s+([A-Za-z'\s]+?)\s*->",
                              re.IGNORECASE)
# Newbie errands never carry spoils/runestones (per supporting docs/VN.txt),
# so the acceptable metrics are daler plus the 6 goods actually seen there.
NEWBIE_METRICS = ("daler", "timber", "iron", "furs", "fish", "grain",
                  "mead")


def parse_newbie_board(lines):
    """A captured `vmission newbie` readout -> ([{id, town, fetch_town,
    daler, goods: [(qty, good), ...], rep_min, rep_max}, ...], quota_used,
    quota_max)."""
    text = " ".join(lines)
    ids = [(m.start(), int(m.group(1)))
           for m in _MISSION_ID_RE.finditer(text)]
    missions = []
    for i, (pos, mid) in enumerate(ids):
        end = ids[i + 1][0] if i + 1 < len(ids) else len(text)
        chunk = text[pos:end]
        dm = _NEWBIE_DALER_RE.search(chunk)
        if not dm:
            continue
        goods = [(int(q), _canon_good(g))
                 for q, g in _MISSION_GOOD_RE.findall(chunk)]
        rm = _NEWBIE_REP_RE.search(chunk)
        tm = _MISSION_TOWN_RE.search(chunk)
        fm = _NEWBIE_FETCH_RE.search(chunk)
        missions.append({
            "id": mid,
            "town": tm.group(1).strip() if tm else f"Mission {mid}",
            "fetch_town": fm.group(1).strip() if fm else "?",
            "daler": int(dm.group(1).replace(",", "")),
            "goods": goods,
            "rep_min": int(rm.group(1)) if rm else 0,
            "rep_max": int(rm.group(2)) if rm else 0,
        })
    qm = _MISSION_QUOTA_RE.search(text)
    used, maxq = (int(qm.group(1)), int(qm.group(2))) if qm else (0, 0)
    return missions, used, maxq


def _newbie_value(mission, metric):
    """The amount of `metric` (daler, or a trade-good name) one errand
    yields - 0 for a good it doesn't carry. For 'daler' this is the 'net'
    value (daler plus the market value of the goods awarded, annotated by
    the caller from a `vtrade prices` capture), falling back to the raw
    daler when no prices were captured."""
    if metric == "daler":
        return mission.get("net", mission["daler"])
    return sum(q for q, g in mission["goods"] if g == metric)


def best_newbie_picks(missions, max_picks, metric="daler"):
    """Top `max_picks` newbie errands ranked by total `metric` yield (one
    of NEWBIE_METRICS; 'daler' means net daler - see _newbie_value).
    Unlike best_mission_picks there's no stock to conflict over - newbie
    errands are free - so the best set is just the highest-yield entries,
    capped at the remaining daily quota. Ties broken by daler. Returns
    (chosen, total_metric, total_daler)."""
    max_picks = max(0, min(max_picks, len(missions)))
    chosen = sorted(
        missions, key=lambda m: (-_newbie_value(m, metric), -m["daler"])
    )[:max_picks]
    return (chosen, sum(_newbie_value(m, metric) for m in chosen),
            sum(m["daler"] for m in chosen))


def pretty_name(name):
    """'trading_post' -> 'Trading Post'."""
    return name.replace("_", " ").title()


def fmt_secs(value):
    """Seconds -> compact 'Dd Hh' / 'Hh Mm' / 'Mm Ss' / 'Ss', matching the
    reference client (e.g. 7508 -> '2h5m', 1188 -> '19m48s')."""
    try:
        s = int(value)
    except (ValueError, TypeError):
        return str(value)
    if s <= 0:
        return "0s"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


# Freshness bands: (min%, label, color). Thresholds reproduce the
# reference screenshot's tiers (100=fresh, 90=slt.stale, 78=stale,
# 62=old, 45=very old) and bar colors (green/green/cyan/blue/red).
FRESH_BANDS = [
    (95, "fresh", "#46c246"),
    (85, "slt. stale", "#7fbf4f"),
    (70, "stale", "#3fb8c2"),
    (55, "old", "#4f7fd6"),
    (0, "very old", "#d65151"),
]


def freshness_band(pct):
    for lo, label, color in FRESH_BANDS:
        if pct >= lo:
            return label, color
    return "very old", "#d65151"


def fresh_bar(pct, width=12):
    fill = max(0, min(width, round(pct / 100 * width)))
    return "█" * fill + "░" * (width - fill)


CITY_KEYS = ("DALER", "GOD_POWER", "WSTOCK", "CELLAR", "REFINERY",
             "PRODUCTION", "BUILDINGS", "MONUMENTS", "THRALLS", "BLOT")

# Carts/market moved off the City tab to the Trade tab in the 2026-07
# feed expansion (ROUTES/RBUILD gave trade its own tab's worth of data).
TRADE_KEYS = ("CARTS", "CIDLE", "MARKET", "INCOMING", "ROUTES", "RBUILD",
              "SUPG", "CUPG")

RAIDS_KEYS = ("SHIPS", "RAIDLOG", "RTARGETS")

# SHPLOTS/SCIVICS/SCONSUME/PATROL/STAFF/BDMG are named in the 2026-07
# guild help doc with no field format; shown raw until captured.
PEOPLE_KEYS = ("SETTLERS", "SETTLERX", "HIRD", "VARANG", "RAID",
               "MISSIONS", "ERRAND", "GARRISON", "SACTIONS", "SHPLOTS",
               "SCIVICS", "SCONSUME", "PATROL", "STAFF", "BDMG",
               "THRALL_FOLLOWER")

FARM_KEYS = ("FARM",)


# --- Trade tab decoders (CARTS/CIDLE/ROUTES/RBUILD) --------------------
def parse_carts(value):
    """CARTS -> list of cart dicts. 2026-07 doc field order:
    mode|good|village|secs|amt|half_in|quality|cart_id|tier|dur|cap|
    escort|legs. legs (route carts only) is the multi-stop plan joined
    with '!', and its per-leg fields REUSE '|' (mode|good|amt|village),
    so the first 12 pipe fields are fixed and everything after belongs
    to legs."""
    out = []
    for entry in value.split(";"):
        if not entry:
            continue
        p = entry.split("|")
        if len(p) < 8:
            continue
        cart = {"mode": p[0], "good": p[1], "village": p[2],
                "secs": field(p, 3, ""), "amt": field(p, 4, ""),
                "half_in": field(p, 5, ""), "quality": field(p, 6, ""),
                "cart_id": field(p, 7, "?"), "tier": field(p, 8, "?"),
                "dur": field(p, 9, "?"), "cap": field(p, 10, "?"),
                "escort": field(p, 11, "0"), "legs": []}
        for leg in "|".join(p[12:]).split("!"):
            lf = leg.split("|")
            if len(lf) >= 4:
                cart["legs"].append((lf[0], lf[1], lf[2], lf[3]))
        out.append(cart)
    return out


def parse_routes(value):
    """ROUTES 'vid|vname|road_tier|fort_tier|road_maint|fort_maint;...'
    -> per-village route infrastructure dicts (tiers 0-5, maint 0-100)."""
    out = []
    for f in split_entries(value):
        if len(f) < 6:
            continue
        try:
            out.append({"vid": int(f[0]), "name": f[1],
                        "road": int(f[2]), "fort": int(f[3]),
                        "road_maint": int(f[4]), "fort_maint": int(f[5])})
        except ValueError:
            continue
    return out


def parse_rbuild(value):
    """RBUILD 'vid|vname|kind|tier|mats_total|mats_done|secs_left|
    total_secs|mat_detail;...' -> roads/forts under construction.
    secs_left: -1=awaiting materials, 0=finalizing, >0=remaining;
    mat_detail reuses the BUILDS 'good:done/need,...' form."""
    out = []
    for f in split_entries(value):
        if len(f) < 8:
            continue
        try:
            out.append({"vid": f[0], "name": f[1], "kind": f[2],
                        "tier": f[3], "mats_total": int(f[4]),
                        "mats_done": int(f[5]), "secs_left": int(f[6]),
                        "total_secs": int(f[7]),
                        "mats": parse_build_res(field(f, 8, ""))})
        except ValueError:
            continue
    return out


# --- Raids tab decoders (SHIPS/RAIDLOG/RTARGETS) -----------------------
def parse_ships(value):
    """SHIPS -> list of ship dicts. 2026-07 doc field order (replaces the
    earlier best-effort guess that had load/cap at positions 5/6 - those
    are actually ship id and crew):
    name|tier|state|target|secs|sid|crew|convoy|convoy_size|convoy_bonus|
    saga_title|saga_raids|held. held=1 means kept back from raid all /
    auto-raid (e.g. a ship reserved for voyages)."""
    out = []
    for f in split_entries(value):
        if len(f) < 3:
            continue
        out.append({"name": f[0], "tier": field(f, 1, "?"),
                    "state": f[2], "target": field(f, 3, ""),
                    "secs": field(f, 4, ""), "sid": field(f, 5, ""),
                    "crew": field(f, 6, ""), "convoy": field(f, 7, "0"),
                    "convoy_size": field(f, 8, ""),
                    "convoy_bonus": field(f, 9, ""),
                    "saga_title": field(f, 10, ""),
                    "saga_raids": field(f, 11, ""),
                    "held": field(f, 12, "0")})
    return out


def parse_raidlog(value):
    """RAIDLOG 'ship|target|daler|thralls|lost|goods;...' -> recent raids,
    oldest first (up to 15). goods is 'good:qty,good:qty' (same form as
    BUILDINGS, so parse_buildings decodes it); empty on a loss."""
    out = []
    for f in split_entries(value):
        if len(f) < 5:
            continue
        try:
            out.append({"ship": f[0], "target": f[1], "daler": int(f[2]),
                        "thralls": int(f[3]), "lost": f[4] == "1",
                        "goods": parse_buildings(field(f, 5, ""))})
        except ValueError:
            continue
    return out


def parse_rtargets(value):
    """RTARGETS 'lineage|historical' -> (lineage, historical), each a
    list of (target_name, good1, good2). Static raid-target data."""
    groups = value.split("|")

    def grp(s):
        out = []
        for e in s.split(";"):
            p = e.split(":")
            if len(p) >= 3 and p[0]:
                out.append((p[0], p[1], p[2]))
        return out

    return grp(field(groups, 0, "")), grp(field(groups, 1, ""))


def parse_farm(value):
    """FARM 'meta|weather_mod;coord|shroom_id|time_left|fertilized|
    wilt_left;...' -> (weather_mod, [{coord, shroom_id, time_left,
    fertilized, wilt_left}, ...]), per the guild help doc's FARM KEY
    section. wilt_left is -1=growing, 0=wilted, >0=seconds to wilt;
    time_left==0 means the plot is READY. Untested against a live
    capture (FARM has never appeared non-empty in any log so far -
    the reference character has no farm plots built)."""
    weather_mod = None
    plots = []
    for f in split_entries(value):
        if f and f[0] == "meta":
            try:
                weather_mod = int(field(f, 1, "0"))
            except ValueError:
                weather_mod = 0
            continue
        if len(f) < 5:
            continue
        try:
            plots.append({
                "coord": f[0], "shroom_id": f[1],
                "time_left": int(f[2]), "fertilized": f[3] == "1",
                "wilt_left": int(f[4]),
            })
        except ValueError:
            continue
    return weather_mod, plots

# SETTLERX field mapping, PARTIALLY resolved - not from the guild help
# doc (its claimed 13-field order was checked against live data and
# disproven: see git history / the design notes for the gory details),
# but from cross-referencing a raw SETTLERX capture against two `vsettler`
# text readouts taken at the same moments (population 4, first
# developed-settlement data seen):
#   SETTLERX^^|0|0|18|1|100|2|22|4|4|100|48|56|0|-13|27|90|100|0^^
#   `vsettler status`: Sustenance 90% / Employment 100% / Security 48% /
#     Dignity 56%, Housing cap 18 (1 plots, avg T1.00).
#   `vsettler community`: Jobs 22 total/4 employed, Market staffed 4,
#     Happiness mult x1.00, Community net -13/tick, Upkeep 27/tick.
# (Mood 73% is NOT in SETTLERX at all - it's SETTLERS field 1, confirmed
# against the same capture: SETTLERS^^4|73|0|212|0^^, population=field0,
# mood=field1.) That pins down 12 of the 18 data fields by exact value
# match. employed/market_staffed (indices 8/9) read identically (4 and 4)
# in this capture, so which index is which is INFERRED from the doc's
# "jobs|employed|market_staffed" order rather than independently
# distinguished - every other confirmed group (housing cap/plots/avg;
# security/dignity) has matched the doc's *relative* order even though
# its absolute offsets were wrong, so this follows the same pattern, but
# treat employed vs market_staffed as unconfirmed until a capture where
# they differ. Two more (indices 1,2) confirmed 2026-06-20: user was
# running the 'open_gates' edict (doc: "6h duration, 18h cooldown") and
# read index1=21498, index2=64698 - both exactly 102s short of 21600 (6h)
# and 64800 (18h), i.e. both counting down from the same activation
# moment. index1 = edict duration remaining, index2 = edict cooldown
# remaining (the wire never names WHICH edict - that's only known from
# what the player themselves activated). The remaining 3 (indices 6,13,18,
# all read 0 so far) are still genuinely unidentified - flourishing is in
# there somewhere, but can't be picked out from an all-zero field.
SETTLERS_POPULATION, SETTLERS_MOOD = 0, 1

SETTLERX_EDICT_REMAINING, SETTLERX_EDICT_COOLDOWN = 1, 2
SETTLERX_HOUSING_CAP, SETTLERX_HOUSING_PLOTS = 3, 4
SETTLERX_HOUSING_AVG = 5
SETTLERX_JOBS = 7
SETTLERX_EMPLOYED, SETTLERX_MARKET_STAFFED = 8, 9      # order inferred
SETTLERX_HAPPINESS_MULT = 10
SETTLERX_SECURITY, SETTLERX_DIGNITY = 11, 12
SETTLERX_COMMUNITY_NET, SETTLERX_UPKEEP = 14, 15
SETTLERX_SUSTENANCE, SETTLERX_EMPLOYMENT = 16, 17

# Confirmed 0-100 percentage fields, rendered as bars (Mood comes from
# SETTLERS, not this list - see _render_people).
SETTLERX_BARS = [("Sustenance", SETTLERX_SUSTENANCE),
                 ("Employment", SETTLERX_EMPLOYMENT),
                 ("Security", SETTLERX_SECURITY),
                 ("Dignity", SETTLERX_DIGNITY)]

SETTLERX_KNOWN_INDICES = frozenset(
    (SETTLERX_EDICT_REMAINING, SETTLERX_EDICT_COOLDOWN,
     SETTLERX_HOUSING_CAP, SETTLERX_HOUSING_PLOTS, SETTLERX_HOUSING_AVG,
     SETTLERX_JOBS, SETTLERX_EMPLOYED, SETTLERX_MARKET_STAFFED,
     SETTLERX_HAPPINESS_MULT, SETTLERX_SECURITY, SETTLERX_DIGNITY,
     SETTLERX_COMMUNITY_NET, SETTLERX_UPKEEP, SETTLERX_SUSTENANCE,
     SETTLERX_EMPLOYMENT))


def _settlerx_int(fields, idx):
    try:
        return int(field(fields, idx, "0"))
    except ValueError:
        return 0


# SACTIONS cracked 2026-06-20: NOT the doc's 'id|timer|cooldown' format -
# it's a flat 6-number list, one slot per action in this fixed order,
# each holding the REMAINING seconds on that action if it's currently
# active (0 = inactive/available). Confirmed live: a `vsettler community`
# readout showed only "Watch: 2h 2m" active (= 7320s) while SACTIONS read
# "0|7350|0|0|0|0" at nearly the same moment (slot 1 = Watch); a later
# capture read "0|7270|0|0|0|0" - slot 1 counting down, confirming it's a
# countdown not a cooldown-elapsed counter.
SACTIONS_NAMES = ("assembly", "watch", "crafts", "feast", "relief",
                  "works")


def parse_sactions(value):
    """SACTIONS value -> [(name, remaining_seconds), ...] in
    SACTIONS_NAMES order, zipped positionally - see the comment above."""
    parts = (value or "").split("|")
    out = []
    for i, name in enumerate(SACTIONS_NAMES):
        try:
            secs = int(field(parts, i, "0"))
        except ValueError:
            secs = 0
        out.append((name, secs))
    return out


# --- People-tab settler/personnel packets, field meanings supplied by
# the user 2026-07-11 (GAME FACTs, not from the guild help doc) ---------
def parse_patrol(value):
    """PATROL 'count|secs' -> (hirdmadr on patrol, seconds until their
    patrol shift ends), or None if unparseable."""
    f = value.split("|")
    try:
        return int(f[0]), int(field(f, 1, "0"))
    except (ValueError, IndexError):
        return None


def parse_semi_pairs(value):
    """'name:val;name:val;...' -> [(name, int), ...]. Covers SCIVICS
    (civic building -> tier) and SCONSUME (upkeep good -> amount)."""
    out = []
    for pair in value.split(";"):
        if ":" not in pair:
            continue
        name, num = pair.rsplit(":", 1)
        try:
            out.append((name, int(num)))
        except ValueError:
            continue
    return out


def parse_shplots(value):
    """SHPLOTS 'n|n|n|n' -> [(tier, count), ...] housing plot counts for
    T1..T4 (e.g. '3|1|0|0' = three T1 plots and one T2)."""
    out = []
    for i, tok in enumerate(value.split("|")):
        try:
            out.append((i + 1, int(tok)))
        except ValueError:
            continue
    return out


# STAFF stat order per the user: combat, trade, craft, sea, wild, land,
# charm.
STAFF_STATS = ("combat", "trade", "craft", "sea", "wild", "land", "charm")

# STAFF loyalty scale, 1-5 (GAME FACT from the user, 2026-07-11).
STAFF_LOYALTY = {5: "Devoted", 4: "Loyal", 3: "Steady", 2: "Uneasy",
                 1: "Shaken"}


def parse_staff(value):
    """STAFF 'name|assigned|specialty|c,t,c,s,w,l,ch|trait|loyalty|age|
    ?;...' -> vroster dicts. trait '0' means none ('Taskmaster' etc
    otherwise); loyalty is 1-5 (see STAFF_LOYALTY), 'veteran' at index 6
    is the age band. Index 7 (seen 0) is still UNIDENTIFIED, kept raw."""
    out = []
    for f in split_entries(value):
        if len(f) < 4:
            continue
        stats = []
        for name, tok in zip(STAFF_STATS, f[3].split(",")):
            try:
                stats.append((name, int(tok)))
            except ValueError:
                continue
        trait = field(f, 4, "0")
        try:
            loyalty = int(field(f, 5, ""))
        except ValueError:
            loyalty = None
        out.append({"name": f[0], "assigned": f[1], "specialty": f[2],
                    "stats": stats,
                    "trait": "" if trait == "0" else trait,
                    "loyalty": loyalty,
                    "age": field(f, 6, ""),
                    "unknown2": field(f, 7, "")})
    return out


def stat_band(pct):
    if pct < 34:
        return "stat_lo"
    if pct < 67:
        return "stat_mid"
    return "stat_hi"


# ======================================================================
# Status window
# ======================================================================
# Tab layout: the reference client's two rows of five, plus the Trade
# and Raids tabs added for the 2026-07 feed expansion (carts/routes and
# the raid log outgrew the City tab).
TABS = [["Stats", "City", "Trade", "Farm", "Builds", "People"],
        ["Goods", "Map", "Bonds", "Ranks", "Raids", "Sea"]]
TAB_FEED = {
    "Stats": "mip_extra", "City": "mip_city", "Trade": "mip_city",
    "Farm": "mip_city", "Builds": "mip_city", "People": "mip_city",
    "Goods": "mip_trade_goods", "Map": "mip_map", "Bonds": "mip_city",
    "Ranks": "mip_city", "Raids": "mip_city", "Sea": "mip_voyage",
}

LEVEL_SYM = {3: "+++", 2: "++", 1: "+", 0: "",
             -1: "-", -2: "--", -3: "---"}
LEVEL_COLOR = {3: "#5fd65f", 2: "#7cc869", 1: "#9cbf77", 0: "#888888",
               -1: "#bf9b77", -2: "#cc7a52", -3: "#d65151"}
GOOD_COLOR = {
    "furs": "#d2a679", "fish": "#6bb5c9", "mead": "#d98c8c",
    "runestones": "#9aa3ad", "spoils": "#c79154",
    "timber": "#a8895f", "iron": "#9fb2c2", "grain": "#c9c45f",
    "ore": "#8a7f76", "salted_fish": "#4f93a8", "bread": "#d6b26b",
    "fine_furs": "#e8c9a0", "tools": "#b0b8a0", "gemstones": "#c76bd6",
}

BG = "#0c0c12"
TAB_BG, TAB_FG = "#1a1a26", "#99aacc"
TAB_BG_ON, TAB_FG_ON = "#2b2b40", "#ffffff"


# --- mip_map decoders (Map tab) ---------------------------------------
# The territory feed: VMAPH header (cols|rows|playerX|playerY), VMR## rows
# of single-char terrain symbols, and VMAPL POI list (type|name|x|y|).
# There is NO documented symbol->terrain key, so TERRAIN_COLORS is a best
# guess from char frequency + the reference legend (Tund/Hill/Mtn/Frst/
# Plns/Watr) and is meant to be tuned against the live map.
TERRAIN_COLORS = {
    ".": "#6e6e6e",    # tundra (gray, most common)
    "t": "#2f7d32",    # forest (trees)
    "f": "#1f5d23",    # dense forest
    "p": "#5cc24f",    # plains
    "h": "#b5a52e",    # hills (olive)
    "A": "#9c4a3a",    # mountains (maroon)
    "W": "#3a6bd6",    # water
    "r": "#46b6c2",    # river (cyan)
    "=": "#7fd2dd",    # road / bridge
}
FEATURE_COLOR = "#555560"      # unknown / special symbols (flagged)

# Types per the 2026-07-17 VMAPL wire capture: capital, lineage, player
# (player-owned villages, 5th field = owner), seer/blot/ruins/farm
# landmarks, and the three mentors.
POI_LABEL = {"capital": "Cap", "lineage": "Lin", "settlement": "Set",
             "player": "Vill", "seer": "Seer", "blot": "Blot",
             "ruins": "Ruin", "farm": "Farm", "mentor_ber": "Ber",
             "mentor_see": "See", "mentor_jarl": "Jarl"}
POI_COLOR = {"capital": "#e8902e", "lineage": "#d8704a",
             "settlement": "#e0d040", "player": "#e0d040",
             "mentor_ber": "#c065c0", "mentor_see": "#c065c0",
             "mentor_jarl": "#c065c0"}
DEFAULT_POI_COLOR = "#dddddd"
PLAYER_COLOR = "#ffffff"

MAP_LEGEND_TERRAIN = [("Tund", "#6e6e6e"), ("Hill", "#b5a52e"),
                      ("Mtn", "#9c4a3a"), ("Frst", "#2f7d32"),
                      ("Plns", "#5cc24f"), ("Watr", "#3a6bd6")]
MAP_LEGEND_POI = [("Cap", "#e8902e"), ("Lin", "#d8704a"),
                  ("Set", "#e0d040"), ("You", "#ffffff")]


def parse_vmaph(value):
    """'cols|rows|playerX|playerY' -> (cols, rows, px, py) ints, or
    (0,0,None,None) if unparseable."""
    f = value.split("|")
    try:
        cols, rows = int(f[0]), int(f[1])
    except (IndexError, ValueError):
        return 0, 0, None, None
    try:
        px, py = int(f[2]), int(f[3])
    except (IndexError, ValueError):
        px = py = None
    return cols, rows, px, py


def collect_vmr(state, rows):
    """Gather VMR00..VMR(rows-1) terrain rows from the merged state."""
    out = []
    for i in range(rows):
        out.append(state.get("VMR%02d" % i, ""))
    return out


def parse_vmapl(value):
    """'type|name|x|y|extra;...' -> [(type, name, x, y), ...]."""
    out = []
    for f in split_entries(value):
        if len(f) < 4:
            continue
        try:
            out.append((f[0], f[1], int(f[2]), int(f[3])))
        except ValueError:
            continue
    return out


# --- coordinate navigation on the guild map ---------------------------
# Identity on this map is (x,y), not the (shared) room vnum, so navigation
# is keyed on coordinates. Movement is the MUD's own 4-connected grid graph
# from MEE (east edges, '1'=open between x,x+1) and MES (south edges,
# between y,y+1). Confirmed directions: n=y-1, s=y+1, e=x+1, w=x-1.
def build_graph(state):
    """-> (cols, rows, mee, mes, (px, py)) or None if no map loaded.
    px/py may be None if the header lacked a position."""
    cols, rows, px, py = parse_vmaph(state.get("VMAPH", ""))
    if cols <= 0 or rows <= 0:
        return None
    mee = [state.get("MEE%02d" % i, "") for i in range(rows)]
    mes = [state.get("MES%02d" % i, "") for i in range(rows)]
    return cols, rows, mee, mes, (px, py)


def _neighbors(x, y, cols, rows, mee, mes):
    out = []
    row = mee[y] if y < len(mee) else ""
    if x + 1 < cols and x < len(row) and row[x] == "1":
        out.append(("e", x + 1, y))
    if x - 1 >= 0 and x - 1 < len(row) and row[x - 1] == "1":
        out.append(("w", x - 1, y))
    if y + 1 < rows and y < len(mes) and x < len(mes[y]) \
            and mes[y][x] == "1":
        out.append(("s", x, y + 1))
    if y - 1 >= 0 and y - 1 < len(mes) and x < len(mes[y - 1]) \
            and mes[y - 1][x] == "1":
        out.append(("n", x, y - 1))
    return out


def pathfind(cols, rows, mee, mes, start, goal):
    """BFS over the MEE/MES grid graph. Returns the shortest list of
    n/s/e/w moves, [] if already there, or None if unreachable."""
    import collections
    if start == goal:
        return []
    prev = {start: None}
    q = collections.deque([start])
    while q:
        cur = q.popleft()
        if cur == goal:
            break
        for d, nx, ny in _neighbors(cur[0], cur[1], cols, rows, mee, mes):
            if (nx, ny) not in prev:
                prev[(nx, ny)] = (cur, d)
                q.append((nx, ny))
    if goal not in prev:
        return None
    moves = []
    node = goal
    while prev[node] is not None:
        parent, d = prev[node]
        moves.append(d)
        node = parent
    moves.reverse()
    return moves


def map_landmarks(state):
    """name(lowercased) -> (x, y, label), the POIs from the live VMAPL
    feed. Settlements move, so this is always read fresh off the wire
    rather than cached or overridden by saved marks."""
    out = {}
    for t, name, x, y in parse_vmapl(state.get("VMAPL", "")):
        out[name.lower()] = (x, y, POI_LABEL.get(t, t))
    return out


# --- mip_extra (Stats) + standings/reputation (Bonds/Ranks) ----------
# No reference screenshot for these three tabs, so the layouts are ours.
# Resource abbreviations (KAP/VIG/SEID/...) are shown as the MUD sends
# them - their full names aren't documented. Each NAME pairs with M+NAME
# for its max.
# VIS/KAP/SOE/AUD are GXP POOLS (Visindi/Kappi/Soemd/Audr), not capped
# resources - the MUD's M-prefixed values for them are not real maxes - so
# they're pulled out of the resource bars and shown in their own GXP section
# with the per-skill costs from `vskills`.
STATS_RESOURCES = ["HP", "SP", "VIG", "RAD", "SEID", "THREK"]
STATS_KEYS = ("HP", "FURY", "VIG", "KAP", "GLVL", "LIN", "STFX",
              "DALER", "VIS", "SOE", "AUD")

# GXP pool name (as printed in `vskills`) -> the live mip_extra key that
# carries its current points between vskills reads.
POOL_MIP = {"Visindi": "VIS", "Kappi": "KAP", "Soemd": "SOE", "Audr": "AUD"}

# `vskills` readout parsing. Lines are framed in decorative -~*...*~- borders;
# the parser strips the frame and reads three kinds of content line:
#   Vegr <Tree>            -> a new skill tree
#   <Pool>: <n> points     -> the tree's GXP pool name + current points
#   <Name> ..... [ L ( saga )]  [ daler ]  -> a skill (a leading '+' = child)
# plus the top "Daler: <n>" total. See client.cmd_vskills / viking_scan_line.
_VSK_SKILL_RE = re.compile(
    r"(\+\s*)?([A-Za-z]+(?: [A-Za-z]+)*)\s*\.{2,}\s*"
    r"\[\s*(\d+)\s*\(\s*([\d,]+|max)\s*\)\]"
    r"\s*(?:\[\s*([\d,]+)\s*\])?")
_VSK_TREE_RE = re.compile(r"^Vegr\s+(\w+)")
_VSK_POOL_RE = re.compile(r"^([A-Z][a-z]+):\s*([\d,]+)\s*points")
_VSK_DALER_RE = re.compile(r"^Daler:\s*([\d,]+)")
# Capture ends on the "Trees: ..." footer (detected in viking_scan_line).


def _vsk_num(s):
    return int(s.replace(",", "")) if s else 0


def parse_vskills(lines):
    """Parse a captured `vskills` readout into ordered trees. Returns
    {"daler": int, "trees": [{"name", "pool", "points", "skills":
    [{"name", "level", "saga", "daler", "child"}]}]}. saga is None for a
    maxed skill ('[20 (   max)]'); skill names can be multi-word
    ('Hrodrs Orka'). Decorative borders and separators are ignored;
    partial/garbled lines that don't match are skipped, so it's safe to
    feed the raw capture."""
    daler = 0
    trees = []
    cur = None
    for raw in lines:
        s = re.sub(r"^[\s\-~*]+", "", raw)        # strip the left -~* frame
        s = re.sub(r"[\s\-~*]+$", "", s)          # and the right *~- frame
        if not s:
            continue
        m = _VSK_SKILL_RE.search(s)
        if m and cur is not None:
            saga = (None if m.group(4) == "max"
                    else _vsk_num(m.group(4)))
            cur["skills"].append({
                "name": m.group(2), "level": int(m.group(3)),
                "saga": saga, "daler": _vsk_num(m.group(5)),
                "child": bool(m.group(1))})
            continue
        mt = _VSK_TREE_RE.match(s)
        if mt:
            cur = {"name": mt.group(1), "pool": None, "points": 0,
                   "skills": []}
            trees.append(cur)
            continue
        mp = _VSK_POOL_RE.match(s)
        if mp and cur is not None and cur["pool"] is None:
            cur["pool"] = mp.group(1)
            cur["points"] = _vsk_num(mp.group(2))
            continue
        md = _VSK_DALER_RE.match(s)
        if md:
            daler = _vsk_num(md.group(1))
    return {"daler": daler, "trees": trees}
RANK_KEYS = ("VREP",)
BONDS_KEYS = ("STANDINGS", "BONDS")

STANDING_COLOR = {
    "allied": "#46d246", "friendly": "#7cc869", "neutral": "#9aa3ad",
    "wary": "#e0b94a", "hostile": "#d68a4a", "hated": "#d65151",
}

# --- mip_voyage decoders (Sea tab) ------------------------------------
# Validated against logs/mip_20260614_112527.log + the reference Sea tab
# screenshot (supporting docs/viking guild sea tab.png). The active voyage
# rides in VOYAGE (a pipe record), its sea map in VCR00..VCR15 rows under a
# VCHH header (w|h|mode), the planned route in VQPATH, the event log in
# VSAGA/VMEM. The raid fleet roster (SHIPS) shows on the Raids tab, so
# the Sea tab focuses on the active voyage, its chart, and its spoils.
VOYAGE_KEYS = ("VOYAGE", "VCHH", "VQPATH", "VSAGA", "VMEM", "LONGSHIP",
               "VOYAGE_WAIT", "VRESOLVE", "VBOONS", "VSPOILS", "VGOODS",
               "VAIDS", "VRUNES", "VRELICS", "VCURIOS")

# The five secured-spoils list keys, all 'name|count;...' records.
SPOILS_KEYS = (("Goods", "VGOODS"), ("Aids", "VAIDS"),
               ("Runestones", "VRUNES"), ("Relics", "VRELICS"),
               ("Curiosities", "VCURIOS"))


def parse_voyage(value):
    """VOYAGE pipe-record -> labelled dict, per the 2026-07 guild help
    doc's authoritative field order (which corrected the earlier
    provisional indices: 16 is steps_sailed not renown, 21 is
    paused_type, weather/renown sit at 22/23):
    state|ship_id|ship_name|contract_name|contract_type|danger|x|y|width|
    height|hull|morale|supplies|hull_stress|crew_alive|crew_max|
    steps_sailed|next_move_in|threat_name|threat_level|threat_pressure|
    paused_type|weather|ship_renown|captain_style|ship_identity|
    crew_traits|ship_traits"""
    f = value.split("|")
    g = lambda i: f[i].strip() if 0 <= i < len(f) else ""
    return {
        "state": g(0), "ship": g(2), "contract": g(3), "type": g(4),
        "danger": g(5), "col": g(6), "row": g(7),
        "grid_w": g(8), "grid_h": g(9),
        "hull": g(10), "morale": g(11), "supplies": g(12),
        "stress": g(13), "crew": g(14), "crew_max": g(15),
        "steps": g(16), "next": g(17),
        "threat": g(18), "threat_lvl": g(19), "pressure": g(20),
        "paused": g(21), "weather": g(22), "renown": g(23),
        "captain": g(24), "identity": g(25),
        "crew_desc": g(26), "traits": g(27),
    }


def parse_longship(value):
    """LONGSHIP 'sid|name|tier|state|target|return_in_secs|crew|
    hired_crew|safe|voyage_rep|voyage_identity|captain_style|crew_traits|
    ship_traits;...' -> voyage-side fleet dicts (2026-07 doc)."""
    out = []
    for f in split_entries(value):
        if len(f) < 4:
            continue
        out.append({"sid": f[0], "name": f[1], "tier": f[2],
                    "state": f[3], "target": field(f, 4, ""),
                    "return_in": field(f, 5, ""), "crew": field(f, 6, ""),
                    "hired": field(f, 7, ""), "safe": field(f, 8, ""),
                    "rep": field(f, 9, ""), "identity": field(f, 10, ""),
                    "captain": field(f, 11, ""),
                    "crew_traits": field(f, 12, ""),
                    "ship_traits": field(f, 13, "")})
    return out


def parse_counts(value):
    """'name|count;...' (the VGOODS/VAIDS/VRUNES/VRELICS/VCURIOS spoils
    lists) -> [(name, count), ...]."""
    out = []
    for f in split_entries(value):
        if len(f) < 2:
            continue
        try:
            out.append((f[0], int(f[1])))
        except ValueError:
            continue
    return out


def parse_vchh(value):
    """VCHH 'w|h|mode' -> (width, height, mode); defaults to a 16x16 grid."""
    f = value.split("|")
    try:
        w, h = int(f[0]), int(f[1])
    except (IndexError, ValueError):
        w, h = 16, 16
    return w, h, (f[2].strip() if len(f) > 2 else "")


def voyage_chart(state, height=16):
    """Collect VCR00..VCR(height-1) from the merged state into row strings
    (a missing row -> '')."""
    return [state.get(f"VCR{r:02d}", "") for r in range(height)]


def parse_vqpath(value):
    """VQPATH -> [(col, row), ...] waypoints (queued route). Two wire
    forms are accepted: the 2026-07 doc's cell labels ('A01,B02' - row
    letter + 1-based column, matching cell_label) and the earlier flat
    'c,r,c,r' number pairs."""
    toks = [t.strip() for t in value.split(",") if t.strip()]
    labels = []
    for t in toks:
        m = re.fullmatch(r"([A-Za-z])(\d+)", t)
        if not m:
            labels = None
            break
        labels.append((int(m.group(2)) - 1,
                       ord(m.group(1).upper()) - ord("A")))
    if labels is not None:
        return labels
    nums = [n for n in toks if n.lstrip("-").isdigit()]
    return [(int(nums[i]), int(nums[i + 1]))
            for i in range(0, len(nums) - 1, 2)]


def find_ship_cell(rows):
    """(row_idx, col_idx) of the 'S' marker in the chart, or None."""
    for r, line in enumerate(rows):
        c = line.find("S")
        if c >= 0:
            return r, c
    return None


def cell_label(row_idx, col_idx):
    """Grid cell -> reference-client label: row letter A.. + 1-based col."""
    return f"{chr(ord('A') + row_idx)}{col_idx + 1:02d}"


# Sea-chart glyphs: raw wire char -> (display char, color-tag suffix). 'O'
# (open sea on the wire) renders as the reference client's '.'; every other
# glyph is shown verbatim, matching the screenshot legend. Unknown chars
# fall back to dim.
SEA_GLYPH = {
    "O": (".", "sea"), ".": (".", "sea"), "#": ("#", "unrev"),
    "S": ("S", "ship"), "+": ("+", "path"), ">": (">", "dest"),
    "I": ("I", "island"), "H": ("H", "harbor"), "W": ("W", "wreck"),
    "T": ("T", "storm"), "F": ("F", "fog"), "X": ("X", "obj"),
    "*": ("*", "mist"), "~": ("~", "current"), "=": ("=", "current"),
    "-": ("-", "dead"), "?": ("?", "unknown"),
}
SEA_TAGS = {
    "sea": "#4a5a6a", "unrev": "#2e2f36", "fog": "#777777",
    "ship": "#ffffff", "path": "#46c246", "dest": "#e0d040",
    "island": "#9c7a3a", "harbor": "#e8902e", "wreck": "#d65151",
    "storm": "#b06bd6", "obj": "#e0d040", "mist": "#46c246",
    "current": "#6fb6d6", "dead": "#666666", "unknown": "#888855",
}
SEA_LEGEND = ["S ship", "+ path", "> dest", "# unrevealed", ". sea",
              "I island", "? unknown", "H harbor", "W wreck", "T storm",
              "X objective", "* mist", "~ stormbelt", "= current",
              "- deadwater"]


def _pct(v):
    """Render a numeric field as a percentage; pass non-numbers through."""
    return f"{v}%" if str(v).isdigit() else (v or "-")


# The Viking hpbar. Source 1 (frequent): the BBE resource keys when
# mip_extra is on. Source 2 (always-on fallback): the FFF 'hpbar1' text
# 'H[hp|max] S[seid|max] V[vig|max] R[rad|max] F[fury] C[chain]'. Both
# feed the same self.vitals keys, which the Viking vitals config (in
# vikings.json) maps to the HP/Seid/Vig/Rad bars.
VITALS_MAP = {
    "HP": "hp", "MHP": "hpmax", "SEID": "seid", "MSEID": "seidmax",
    "VIG": "vig", "MVIG": "vigmax", "RAD": "rad", "MRAD": "radmax",
}


def vitals_from_state(state):
    """Pull HP/Seid/Vig/Rad cur+max out of the merged BBE state into the
    lowercase keys the vitals bars read."""
    out = {}
    for src, dst in VITALS_MAP.items():
        if src in state:
            try:
                out[dst] = int(state[src])
            except ValueError:
                pass
    return out


def parse_hpbar(text):
    """FFF 'H[2030|2017(...)] S[366|375] V[..] R[..]' -> vitals dict.
    Always-on fallback for when mip_extra (the BBE numerics) is off."""
    import re
    out = {}
    for letter, name in (("H", "hp"), ("S", "seid"), ("V", "vig"),
                         ("R", "rad")):
        m = re.search(letter + r"\[(\d+)\|(\d+)", text)
        if m:
            out[name] = int(m.group(1))
            out[name + "max"] = int(m.group(2))
    return out


def active_spells(state):
    """Status-bar line: 'Daler: 5,383  Spells: bles 11, rage 3' (DALER
    prefix omitted if unknown; whole string empty if neither is known)."""
    daler = state.get("DALER")
    daler_part = ""
    if daler is not None:
        try:
            daler_part = f"Daler: {int(daler):,}"
        except ValueError:
            daler_part = f"Daler: {daler}"

    fx = parse_effects(state.get("STFX", ""))
    spells_part = ("Spells: " + ", ".join(f"{n} {v}" for n, v in fx)
                   if fx else "")

    return "  ".join(p for p in (daler_part, spells_part) if p)


def parse_meter(value):
    """FURY/STFX style '[----------]grey:' -> (filled, total), counting
    non-empty cells (anything but '-', '.', space) inside the brackets."""
    import re
    m = re.search(r"\[([^\]]*)\]", value)
    if not m:
        return 0, 0
    inner = m.group(1)
    return sum(1 for c in inner if c not in "-. "), len(inner)


def parse_effects(value):
    """STFX '[bles:11]gray:[rage:3]' -> [(name, value), ...]."""
    import re
    return re.findall(r"\[([^:\]]+):([^\]]+)\]", value)


def parse_vrep(value):
    """VREP 'id|name|rep|rank|cur|next;...' (rank/cur/next best-effort)."""
    out = []
    for f in split_entries(value):
        if len(f) < 3:
            continue
        try:
            out.append((int(f[0]), f[1], int(f[2]), field(f, 3, "0"),
                        field(f, 4, "0"), field(f, 5, "0")))
        except ValueError:
            continue
    return out


# Bond level -> tier name (GAME FACT from the user, 2026-07-11).
BOND_LEVELS = {1: "Comrades", 2: "Shield-Brothers", 3: "Blood-Sworn",
               4: "Oathbound"}


def parse_bonds(value):
    """BONDS 'id1|id2|points|level;...' -> [(id1, id2, points, level)].
    A bond pairs two hirdmadr of the personal guard by their HIRD ids;
    points is a hidden progress value, level the resulting bond level
    (e.g. '3|4|194235|1' = hird #3 and #4 at 194,235 points = a level 1
    bond - GAME FACT from the user, 2026-07-11)."""
    out = []
    for f in split_entries(value):
        if len(f) < 4:
            continue
        try:
            out.append((f[0].strip(), f[1].strip(),
                        int(f[2]), int(f[3])))
        except ValueError:
            continue
    return out


def hird_names(state):
    """HIRD id -> name map (HIRD field 0 = id, field 1 = name), for
    resolving the BONDS pair ids to hirdmadr names."""
    out = {}
    for f in split_entries(state.get("HIRD", "")):
        if len(f) >= 2:
            out[f[0].strip()] = f[1]
    return out


def parse_standings(value):
    """STANDINGS 'id|name|value|label|flag;...' (flag=1 = home lineage)."""
    out = []
    for f in split_entries(value):
        if len(f) < 4:
            continue
        try:
            out.append((int(f[0]), f[1], int(f[2]), f[3],
                        field(f, 4, "0")))
        except ValueError:
            continue
    return out


class MapCanvas(tk.Canvas):
    """Draws the VMR terrain grid as scaled colored cells, with POI and
    player markers overlaid. Rescales to fit on resize."""

    def __init__(self, parent, click_cb=None, **kw):
        super().__init__(parent, bg="#08080c", highlightthickness=0, **kw)
        self.grid_rows = []
        self.cols = self.rows = 0
        self.pois = []            # (x, y, color)
        self.player = None        # (x, y)
        self.click_cb = click_cb
        self._cell = 0
        self._ox = self._oy = 0
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Button-1>", self._on_click)
        if click_cb:
            self.configure(cursor="hand2")

    def _on_click(self, e):
        if not (self.click_cb and self._cell):
            return
        cx = (e.x - self._ox) // self._cell
        cy = (e.y - self._oy) // self._cell
        if 0 <= cx < self.cols and 0 <= cy < self.rows:
            self.click_cb(cx, cy)

    def set_data(self, grid_rows, cols, rows, pois, player):
        self.grid_rows = grid_rows
        self.cols, self.rows = cols, rows
        self.pois = pois
        self.player = player
        self.redraw()

    def redraw(self):
        self.delete("all")
        if not self.grid_rows or self.cols <= 0 or self.rows <= 0:
            return
        w = self.winfo_width() or 480
        h = self.winfo_height() or 280
        cell = max(1, min(w // self.cols, h // self.rows))
        ox = (w - cell * self.cols) // 2
        oy = (h - cell * self.rows) // 2
        self._cell, self._ox, self._oy = cell, ox, oy
        for ry, row in enumerate(self.grid_rows):
            for cx, ch in enumerate(row[:self.cols]):
                color = TERRAIN_COLORS.get(ch, FEATURE_COLOR)
                x0, y0 = ox + cx * cell, oy + ry * cell
                self.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                      fill=color, width=0)
        mk = max(cell, 3)
        for px, py, color in self.pois:
            if 0 <= px < self.cols and 0 <= py < self.rows:
                x0, y0 = ox + px * cell, oy + py * cell
                self.create_rectangle(x0, y0, x0 + mk, y0 + mk,
                                      fill=color, outline="#000000")
        if self.player:
            px, py = self.player
            x0, y0 = ox + px * cell, oy + py * cell
            self.create_rectangle(x0, y0, x0 + mk, y0 + mk,
                                  fill=PLAYER_COLOR, outline="#000000")


class VikingStatus(tk.Toplevel):
    """Detached tabbed 'Viking Status' window, fed by the merged BBE
    state dict the client maintains. update_state() is idempotent - the
    client may call it on every BBE packet."""

    def __init__(self, master, fonts=None, on_close=None, walk_cb=None,
                 geometry=None):
        super().__init__(master)
        self.title("Viking Status")
        self.configure(bg=BG)
        self.geometry(geometry or "720x780")
        self.on_close = on_close
        self.walk_cb = walk_cb
        self.protocol("WM_DELETE_WINDOW", self._closed)
        f = fonts or {}
        self.mono = f.get("mono", ("Consolas", 11))
        self.mono_bold = f.get("mono_bold", ("Consolas", 11, "bold"))
        self.state_data = {}
        self.tabs = {}
        self.tab_btns = {}
        self.current = None
        self.goods_txt = None
        self.city_txt = None
        self.trade_txt = None
        self.raids_txt = None
        self.people_txt = None
        self.map_canvas = None
        self.map_poi = None
        self.map_pos = None
        self._map_sig = None
        self.stats_txt = None
        self.vskills = None         # last parsed `vskills` (skill costs)
        self.ranks_txt = None
        self.bonds_txt = None
        self.builds_txt = None
        self.farm_txt = None
        self.sea_txt = None
        self._build()

    # ------------------------------------------------------ construction
    def _build(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(side="top", fill="x")
        for row in TABS:
            rf = tk.Frame(bar, bg=BG)
            rf.pack(fill="x")
            for name in row:
                btn = tk.Label(rf, text=name, bg=TAB_BG, fg=TAB_FG,
                               font=self.mono_bold, padx=10, pady=4,
                               cursor="hand2")
                btn.pack(side="left", fill="x", expand=True,
                         padx=1, pady=1)
                btn.bind("<Button-1>", lambda _e, n=name: self.show(n))
                self.tab_btns[name] = btn

        self.body = tk.Frame(self, bg=BG)
        self.body.pack(side="top", fill="both", expand=True)
        for row in TABS:
            for name in row:
                frame = tk.Frame(self.body, bg=BG)
                self.tabs[name] = frame
                if name == "Goods":
                    self._build_goods(frame)
                elif name == "City":
                    self._build_city(frame)
                elif name == "Trade":
                    self._build_trade(frame)
                elif name == "Raids":
                    self._build_raids(frame)
                elif name == "People":
                    self._build_people(frame)
                elif name == "Map":
                    self._build_map(frame)
                elif name == "Stats":
                    self._build_stats(frame)
                elif name == "Ranks":
                    self._build_ranks(frame)
                elif name == "Bonds":
                    self._build_bonds(frame)
                elif name == "Builds":
                    self._build_builds(frame)
                elif name == "Farm":
                    self._build_farm(frame)
                elif name == "Sea":
                    self._build_sea(frame)
                else:
                    tk.Label(
                        frame, bg=BG, fg="#556070", justify="left",
                        font=self.mono,
                        text=f"\n    {name} - coming soon\n"
                             f"    (will be fed by {TAB_FEED[name]})"
                    ).pack(anchor="nw")
        self.show("Goods")

    def _scrolled_text(self, frame):
        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        txt = tk.Text(frame, bg=BG, fg="#cccccc", font=self.mono,
                      wrap="none", state="disabled", padx=10, pady=8,
                      highlightthickness=0, borderwidth=0,
                      yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.configure(command=txt.yview)
        txt.tag_configure("dim", foreground="#777777")
        txt.tag_configure("num", foreground="#dddddd")
        return txt

    def _build_goods(self, frame):
        # Filter bar: a button per good -> best-places-to-buy/sell view
        # for that good; All resets to the per-city market view.
        self.goods_filter = None
        self.goods_btns = {}
        bar = tk.Frame(frame, bg=BG)
        bar.pack(side="top", fill="x")
        names = ["all"] + list(GOOD_COLOR)
        for row in (names[:8], names[8:]):
            rf = tk.Frame(bar, bg=BG)
            rf.pack(fill="x")
            for name in row:
                btn = tk.Label(rf, text=pretty_name(name), bg=TAB_BG,
                               fg=GOOD_COLOR.get(name, TAB_FG),
                               font=self.mono, padx=4, pady=1,
                               cursor="hand2")
                btn.pack(side="left", fill="x", expand=True,
                         padx=1, pady=1)
                btn.bind("<Button-1>",
                         lambda _e, n=name: self._set_goods_filter(n))
                self.goods_btns[name] = btn
        self.goods_btns["all"].configure(bg=TAB_BG_ON)

        txt = self._scrolled_text(frame)
        txt.tag_configure("hold", foreground="#cdb87a",
                          font=self.mono_bold)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("cycle", foreground="#6fb6d6")
        for good, col in GOOD_COLOR.items():
            txt.tag_configure("good_" + good, foreground=col)
        for lvl, col in LEVEL_COLOR.items():
            txt.tag_configure("lvl%d" % lvl, foreground=col)
        self.goods_txt = txt

    def _set_goods_filter(self, name):
        self.goods_filter = None if name == "all" else name
        active = name if name in self.goods_btns else "all"
        for n, btn in self.goods_btns.items():
            btn.configure(bg=TAB_BG_ON if n == active else TAB_BG)
        self._render_goods(self.state_data.get("TGOODS", ""),
                           self.state_data.get("DCYCLE", ""))

    def _build_city(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("gold", foreground="#e0b94a")
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("cyan", foreground="#6fb6d6")
        for good, col in GOOD_COLOR.items():
            txt.tag_configure("good_" + good, foreground=col)
        for i, (_lo, _label, col) in enumerate(FRESH_BANDS):
            txt.tag_configure("fr%d" % i, foreground=col)
        self.city_txt = txt

    def _build_trade(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("cyan", foreground="#6fb6d6")
        txt.tag_configure("res_ok", foreground="#46c246")
        txt.tag_configure("res_no", foreground="#d65151")
        txt.tag_configure("stat_lo", foreground="#d65151")
        txt.tag_configure("stat_mid", foreground="#e0b94a")
        txt.tag_configure("stat_hi", foreground="#46c246")
        for good, col in GOOD_COLOR.items():
            txt.tag_configure("good_" + good, foreground=col)
        self.trade_txt = txt

    def _build_raids(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("cyan", foreground="#6fb6d6")
        txt.tag_configure("gold", foreground="#e0b94a")
        txt.tag_configure("res_no", foreground="#d65151")
        for good, col in GOOD_COLOR.items():
            txt.tag_configure("good_" + good, foreground=col)
        self.raids_txt = txt

    def _build_people(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("cyan", foreground="#6fb6d6")
        txt.tag_configure("stat_lo", foreground="#d65151")
        txt.tag_configure("stat_mid", foreground="#e0b94a")
        txt.tag_configure("stat_hi", foreground="#46c246")
        self.people_txt = txt

    def _build_map(self, frame):
        legend = tk.Frame(frame, bg=BG)
        legend.pack(side="top", fill="x", padx=8, pady=(6, 2))
        for label, color in MAP_LEGEND_TERRAIN + MAP_LEGEND_POI:
            tk.Label(legend, text="█", bg=BG, fg=color,
                     font=self.mono).pack(side="left")
            tk.Label(legend, text=label + " ", bg=BG, fg="#aaaaaa",
                     font=self.mono).pack(side="left")
        self.map_pos = tk.Label(legend, text="", bg=BG, fg="#6fb6d6",
                                font=self.mono)
        self.map_pos.pack(side="right")

        self.map_canvas = MapCanvas(frame, click_cb=self.walk_cb,
                                    height=260)
        self.map_canvas.pack(side="top", fill="both", expand=True,
                             padx=8, pady=2)

        poi_frame = tk.Frame(frame, bg=BG)
        poi_frame.pack(side="top", fill="both", expand=True)
        sb = tk.Scrollbar(poi_frame)
        sb.pack(side="right", fill="y")
        txt = tk.Text(poi_frame, bg=BG, fg="#cccccc", font=self.mono,
                      wrap="none", state="disabled", padx=10, pady=6,
                      height=10, highlightthickness=0, borderwidth=0,
                      yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.configure(command=txt.yview)
        txt.tag_configure("dim", foreground="#777777")
        for kind, color in POI_COLOR.items():
            txt.tag_configure("poi_" + kind, foreground=color)
        txt.tag_configure("poi_default", foreground=DEFAULT_POI_COLOR)
        self.map_poi = txt

    def _build_stats(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("fury", foreground="#e0703a")
        txt.tag_configure("stat_lo", foreground="#d65151")
        txt.tag_configure("stat_mid", foreground="#e0b94a")
        txt.tag_configure("stat_hi", foreground="#46c246")
        txt.tag_configure("num", foreground="#cccccc")
        txt.tag_configure("gold", foreground="#e0b94a", font=self.mono_bold)
        txt.tag_configure("cost", foreground="#88aacc")
        self.stats_txt = txt

    def _build_ranks(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        self.ranks_txt = txt

    def _build_bonds(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("gold", foreground="#e0b94a")
        txt.tag_configure("cyan", foreground="#6fb6d6")
        for label, color in STANDING_COLOR.items():
            txt.tag_configure("st_" + label, foreground=color)
        self.bonds_txt = txt

    def _build_builds(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("cyan", foreground="#6fb6d6")
        txt.tag_configure("res_ok", foreground="#46c246")
        txt.tag_configure("res_no", foreground="#d65151")
        txt.tag_configure("stat_lo", foreground="#d65151")
        txt.tag_configure("stat_mid", foreground="#e0b94a")
        txt.tag_configure("stat_hi", foreground="#46c246")
        self.builds_txt = txt

    def _build_farm(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("cyan", foreground="#6fb6d6")
        txt.tag_configure("res_ok", foreground="#46c246")
        txt.tag_configure("res_no", foreground="#d65151")
        self.farm_txt = txt

    def _build_sea(self, frame):
        txt = self._scrolled_text(frame)
        txt.tag_configure("sec", foreground="#d79030",
                          font=self.mono_bold)
        txt.tag_configure("val", foreground="#cccccc")
        txt.tag_configure("cyan", foreground="#6fb6d6")
        txt.tag_configure("colhdr", foreground="#667088")
        for suffix, color in SEA_TAGS.items():
            txt.tag_configure("sea_" + suffix, foreground=color)
        self.sea_txt = txt

    # The reference Sea tab's right column starts at ~40 cols.
    SEA_COLW = 40

    def _sea_cols(self, txt, left, right):
        """Two-column key/value block (left + right label:value lists)."""
        for i in range(max(len(left), len(right))):
            used = 0
            if i < len(left):
                lbl, val = left[i]
                txt.insert("end", f"{lbl}: ", "dim")
                txt.insert("end", val or "-", "val")
                used = len(lbl) + 2 + len(val or "-")
            if i < len(right):
                txt.insert("end", " " * max(1, self.SEA_COLW - used), "dim")
                lbl, val = right[i]
                txt.insert("end", f"{lbl}: ", "dim")
                txt.insert("end", val or "-", "val")
            txt.insert("end", "\n")

    def _sea_chart(self, txt, rows, w):
        """Render the VCR sea grid: a col-number header, then each row
        prefixed with its A.. letter, each cell coloured by its glyph."""
        txt.insert("end", "    ", "colhdr")
        for c in range(w):
            txt.insert("end", f"{c + 1:02d} ", "colhdr")
        txt.insert("end", "\n")
        for r, line in enumerate(rows):
            txt.insert("end", f" {chr(ord('A') + r)}  ", "colhdr")
            for c in range(w):
                raw = line[c] if c < len(line) else "#"
                disp, suffix = SEA_GLYPH.get(raw, (raw, "unknown"))
                txt.insert("end", disp + "  ", "sea_" + suffix)
            txt.insert("end", "\n")

    def _render_sea(self, st):
        txt = self.sea_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        voy = st.get("VOYAGE", "")
        w, h, mode = parse_vchh(st.get("VCHH", ""))
        rows = voyage_chart(st, h)
        if not voy and not any(rows):
            txt.insert("end", "\n  No active voyage.\n", "dim")
            txt.insert("end", "  Put a longship to sea, and enable "
                              "'vtoggle mip_voyage' to feed this tab.\n",
                       "dim")
            self._sea_fleet(txt, st)
            txt.configure(state="disabled")
            return

        v = parse_voyage(voy)
        ship = find_ship_cell(rows)
        if ship:
            position = cell_label(*ship)
        elif v["row"].isdigit() and v["col"].isdigit():
            position = cell_label(int(v["row"]), int(v["col"]))
        else:
            position = "?"
        threat = v["threat"] or "-"
        if v["threat"] and v["threat_lvl"]:
            threat = f"{v['threat']} [{v['threat_lvl']}]"

        txt.insert("end", "Voyage\n", "sec")
        self._sea_cols(
            txt,
            [("Ship", v["ship"]), ("Contract", v["contract"]),
             ("Type", v["type"]), ("Danger", v["danger"]),
             ("Hull", _pct(v["hull"])), ("Morale", _pct(v["morale"])),
             ("Stress", _pct(v["stress"])), ("State", v["state"]),
             ("Threat", threat), ("Identity", v["identity"]),
             ("Traits", v["traits"]), ("Crew", v["crew_desc"])],
            [("Position", position), ("Mode", mode.capitalize()),
             ("Renown", v["renown"]),
             ("Crew", f"{v['crew']}/{v['crew_max']}"),
             ("Supplies", _pct(v["supplies"])),
             ("Weather", v["weather"] or "Calm"),
             ("Next", fmt_secs(v["next"]) if v["next"] else "-"),
             ("Pressure", v["pressure"]), ("Captain", v["captain"]),
             ("Steps", v["steps"])])

        paused = (st.get("VOYAGE_WAIT", "") or "").strip() or v["paused"]
        if paused:
            txt.insert("end", f"\nPaused: {paused} node", "sec")
            choices = [c.strip() for c in
                       st.get("VRESOLVE", "").split(",") if c.strip()]
            if choices:
                txt.insert("end", "   choices: ", "dim")
                txt.insert("end", ", ".join(choices), "cyan")
            txt.insert("end", "\n")

        txt.insert("end", "\nChart", "sec")
        txt.insert("end", f"  ({w}x{h}"
                          + (f" {mode}" if mode else "") + ")\n", "dim")
        self._sea_chart(txt, rows, w)
        txt.insert("end", "\n  " + "   ".join(SEA_LEGEND[:8]) + "\n", "dim")
        txt.insert("end", "  " + "   ".join(SEA_LEGEND[8:]) + "\n", "dim")

        path = parse_vqpath(st.get("VQPATH", ""))
        if path:
            txt.insert("end", "\nQueue\n", "sec")
            txt.insert("end", "  " + " -> ".join(
                cell_label(r, c) for c, r in path) + "\n", "cyan")

        spoils_lists = [(label, parse_counts(st.get(key, "")))
                        for label, key in SPOILS_KEYS]
        boons = [b.strip() for b in st.get("VBOONS", "").split(",")
                 if b.strip()]
        if st.get("VSPOILS") or boons or \
                any(items for _l, items in spoils_lists):
            txt.insert("end", "\nSpoils", "sec")
            if st.get("VSPOILS"):
                txt.insert("end", f"   {st['VSPOILS']} daler secured",
                           "sea_dest")
            txt.insert("end", "\n")
            for label, items in spoils_lists:
                if items:
                    txt.insert("end", f"  {label}: ", "dim")
                    txt.insert("end", ", ".join(
                        f"{n} x{c}" for n, c in items) + "\n", "val")
            if boons:
                txt.insert("end", "  Boons: ", "dim")
                txt.insert("end", ", ".join(boons) + "\n", "cyan")

        saga = [s.strip() for s in st.get("VSAGA", "").split(";")
                if s.strip()]
        if saga:
            txt.insert("end", "\nSaga\n", "sec")
            for s in saga:
                txt.insert("end", "  - " + s + "\n", "val")
        mem = [s.strip() for s in st.get("VMEM", "").split(";")
               if s.strip()]
        if mem:
            txt.insert("end", "\nMemories\n", "sec")
            for s in mem:
                txt.insert("end", "  - " + s + "\n", "dim")
        self._sea_fleet(txt, st)
        txt.configure(state="disabled")

    def _sea_fleet(self, txt, st):
        """The LONGSHIP voyage-side fleet roster (crew/rep/traits detail
        the City-side SHIPS key doesn't carry)."""
        fleet = parse_longship(st.get("LONGSHIP", ""))
        if not fleet:
            return
        txt.insert("end", "\nFleet\n", "sec")
        for s in fleet:
            crew = s["crew"]
            if s["hired"] not in ("", "0"):
                crew += f"+{s['hired']}"
            head = f"  {s['name']} T{s['tier']}  {s['state']}"
            self._row(txt, head, "val", f"crew {crew}", "cyan")
            if s["target"]:
                suffix = (f"  ({fmt_secs(s['return_in'])})"
                          if s["return_in"] not in ("", "0") else "")
                txt.insert("end", f"      → {s['target']}{suffix}\n",
                           "dim")
            detail = " · ".join(p for p in (
                s["identity"], s["captain"],
                f"rep {s['rep']}" if s["rep"] else "",
                s["crew_traits"], s["ship_traits"]) if p)
            if detail:
                txt.insert("end", "      " + detail + "\n", "dim")

    # ------------------------------------------------------- tab control
    def show(self, name):
        if self.current == name:
            return
        if self.current:
            self.tabs[self.current].pack_forget()
            self.tab_btns[self.current].configure(bg=TAB_BG, fg=TAB_FG)
        self.tabs[name].pack(fill="both", expand=True)
        self.tab_btns[name].configure(bg=TAB_BG_ON, fg=TAB_FG_ON)
        self.current = name

    # ----------------------------------------------------------- updates
    def update_state(self, state):
        self.state_data = state
        if "TGOODS" in state:
            self._render_goods(state.get("TGOODS", ""),
                               state.get("DCYCLE", ""))
        if self.city_txt is not None and \
                any(k in state for k in CITY_KEYS):
            self._render_city(state)
        if self.trade_txt is not None and \
                any(k in state for k in TRADE_KEYS):
            self._render_trade(state)
        if self.raids_txt is not None and \
                any(k in state for k in RAIDS_KEYS):
            self._render_raids(state)
        if self.people_txt is not None and \
                any(k in state for k in PEOPLE_KEYS):
            self._render_people(state)
        if self.map_canvas is not None and "VMAPH" in state:
            self._render_map(state)
        if self.stats_txt is not None and \
                any(k in state for k in STATS_KEYS):
            self._render_stats(state)
        if self.ranks_txt is not None and \
                any(k in state for k in RANK_KEYS):
            self._render_ranks(state)
        if self.bonds_txt is not None and \
                any(k in state for k in BONDS_KEYS):
            self._render_bonds(state)
        if self.builds_txt is not None and \
                any(k in state for k in BUILD_KEYS):
            self._render_builds(state)
        if self.farm_txt is not None and \
                any(k in state for k in FARM_KEYS):
            self._render_farm(state)
        if self.sea_txt is not None and (
                any(k in state for k in VOYAGE_KEYS)
                or any(k.startswith("VCR") for k in state)):
            self._render_sea(state)

    def _render_goods(self, tgoods, dcycle):
        txt = self.goods_txt
        if txt is None:
            return
        holds = parse_tgoods(tgoods)
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        if self.goods_filter:
            self._render_good_focus(txt, holds, self.goods_filter)
            txt.configure(state="disabled")
            return
        if dcycle:
            parts = dcycle.split("|")
            txt.insert("end", "Demand cycle: ", "dim")
            txt.insert("end", parts[0], "cycle")
            if len(parts) > 1:
                txt.insert("end", f"   {fmt_secs(parts[1])} left", "dim")
            txt.insert("end", "\n\n")
        for hid, goods in holds:
            txt.insert("end", HOLD_NAMES.get(hid, f"Hold {hid}") + "\n",
                       "hold")
            # Only the goods that matter for trading: in demand (score
            # +) or a major export (score -); neutrals hidden.
            goods = [g for g in goods if g[1]]
            if not goods:
                txt.insert("end", "  (neutral market)\n", "dim")
            for i in range(0, len(goods), 2):
                txt.insert("end", "  ")
                self._cell(txt, goods[i])
                if i + 1 < len(goods):
                    txt.insert("end", "    ")
                    self._cell(txt, goods[i + 1])
                txt.insert("end", "\n")
            txt.insert("end", "\n")
        txt.configure(state="disabled")

    def _render_good_focus(self, txt, holds, good):
        """One good across every city: where to buy it cheapest and
        where it sells highest, price-sorted."""
        gtag = "good_" + good
        if gtag not in txt.tag_names():
            gtag = "num"
        rows = []                 # (city, level, sup, dem, buy, sell)
        for hid, goods in holds:
            for g in goods:
                if g[0] == good:
                    rows.append((HOLD_NAMES.get(hid, f"Hold {hid}"),)
                                + g[1:])
        txt.insert("end", pretty_name(good) + "\n\n", gtag)
        if not rows:
            txt.insert("end", "  No market data for this good yet.\n",
                       "dim")
            return

        def sym(level):
            return (f"{LEVEL_SYM.get(level, ''):>3}",
                    "lvl%d" % level if level in LEVEL_COLOR else "num")

        buys = sorted((r for r in rows if r[4] is not None),
                      key=lambda r: r[4])
        txt.insert("end", "Best places to buy\n", "sec")
        if not buys:
            txt.insert("end", "  No prices received.\n", "dim")
        for city, level, sup, _dem, buy, _sell in buys:
            txt.insert("end", f"  {city:<15}", "val")
            txt.insert("end", *sym(level))
            txt.insert("end", "  buy ", "dim")
            txt.insert("end", f"{buy:>4}", "cycle")
            txt.insert("end", "   Sup:", "dim")
            txt.insert("end", f"{sup}\n", "num")

        sells = sorted((r for r in rows if r[5] is not None),
                       key=lambda r: -r[5])
        txt.insert("end", "\nBest places to sell\n", "sec")
        if not sells:
            txt.insert("end", "  No prices received.\n", "dim")
        for city, level, _sup, dem, _buy, sell in sells:
            txt.insert("end", f"  {city:<15}", "val")
            txt.insert("end", *sym(level))
            txt.insert("end", "  sell ", "dim")
            txt.insert("end", f"{sell:>3}", "cycle")
            txt.insert("end", "   Dem:", "dim")
            txt.insert("end", f"{dem}\n", "num")

    def _cell(self, txt, good_tuple):
        # In-demand goods show what the town pays you (sell) and how
        # much it wants; exports show what it charges you (buy) and its
        # stock.
        good, level, sup, dem, buy, sell = good_tuple
        gtag = "good_" + good
        if gtag not in txt.tag_names():
            gtag = "num"
        txt.insert("end", f"{good:<11}", gtag)
        txt.insert("end", f"{LEVEL_SYM.get(level, ''):>3}",
                   "lvl%d" % level if level in LEVEL_COLOR else "num")
        if level > 0:
            txt.insert("end", " Dem:", "dim")
            txt.insert("end", f"{dem:>3}", "num")
            if sell is not None:
                txt.insert("end", " sell ", "dim")
                txt.insert("end", f"{sell:>3}", "cycle")
        else:
            txt.insert("end", " Sup:", "dim")
            txt.insert("end", f"{sup:>3}", "num")
            if buy is not None:
                txt.insert("end", " buy  ", "dim")
                txt.insert("end", f"{buy:>3}", "cycle")

    # --------------------------------------------------------- City tab
    CITY_W = 52        # right-align column for times / counts

    def _row(self, txt, left, left_tag, right, right_tag):
        """One line with left text and a right-aligned value."""
        pad = self.CITY_W - len(left) - len(right)
        txt.insert("end", left, left_tag)
        txt.insert("end", " " * max(1, pad), "dim")
        txt.insert("end", right + "\n", right_tag)

    def _render_city(self, st):
        txt = self.city_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")

        if "DALER" in st:
            txt.insert("end", "Daler: ", "sec")
            daler = st["DALER"]
            try:
                daler = f"{int(daler):,}"
            except ValueError:
                pass
            txt.insert("end", daler + "\n", "gold")

        god = st.get("GOD_POWER", "")
        if god:
            txt.insert("end", "Active God\n", "sec")
            txt.insert("end", "  In Power: ", "dim")
            txt.insert("end", god + "\n", "val")
            if st.get("GOD_POWER_NEXT"):
                self._row(txt, "  Resets In:", "dim",
                          fmt_secs(st["GOD_POWER_NEXT"]), "cyan")
            if st.get("GOD_POWER_FOCUS"):
                self._row(txt, "  Focus:", "dim",
                          st["GOD_POWER_FOCUS"], "val")

        thralls = parse_thralls(st.get("THRALLS", ""))
        follower = parse_thrall_follower(st.get("THRALL_FOLLOWER", ""))
        if thralls or follower:
            total = sum(c for _n, c in thralls)
            txt.insert("end", f"Thralls  [{total}]\n", "sec")
            if follower:
                head = f"  {follower['name']}  L{follower['level']}"
                if follower["status"]:
                    head += f"  {follower['status']}"
                try:
                    xp = (f"{int(follower['xp']):,}/"
                          f"{int(follower['next_xp']):,} xp")
                except ValueError:
                    xp = f"{follower['xp']}/{follower['next_xp']} xp"
                self._row(txt, head, "val",
                          f"{xp}   carry {follower['carried']}/"
                          f"{follower['cap']}", "cyan")
            # Zero counts stay visible: an empty building means either
            # unprioritized or its thralls escaped - both worth seeing.
            for i in range(0, len(thralls), 2):
                for n, c in thralls[i:i + 2]:
                    tag = "val" if c else "dim"
                    txt.insert("end", f"  {n:<17}", tag)
                    txt.insert("end", f"{c:<7}", tag)
                txt.insert("end", "\n")

        if "WSTOCK" in st:
            stock = parse_wstock(st["WSTOCK"])
            total = sum(b[0] for bs in stock.values() for b in bs)
            cap = warehouse_cap(st)
            head = f"Warehouse  [{total}" + \
                (f" / {cap}]" if cap else "]")
            txt.insert("end", head + "\n", "sec")
            if st.get("NEXTTICK"):
                self._row(txt, "  Next stock tick", "dim",
                          fmt_secs(st["NEXTTICK"]), "cyan")
            for good, batches in stock.items():
                gtag = "good_" + good
                if gtag not in txt.tag_names():
                    gtag = "val"
                for i, (amt, fresh, grade) in enumerate(
                        sorted(batches, key=lambda b: -b[1])):
                    band_i = next(j for j, (lo, _l, _c)
                                  in enumerate(FRESH_BANDS) if fresh >= lo)
                    label = grade if grade else FRESH_BANDS[band_i][1]
                    ftag = "fr%d" % band_i
                    txt.insert("end", f"  {good if i == 0 else '':<9}",
                               gtag)
                    txt.insert("end", f"{label:<11}", ftag)
                    txt.insert("end", f"{amt:>5} ", "num")
                    txt.insert("end", fresh_bar(fresh), ftag)
                    txt.insert("end", f"{fresh:>4}%\n", ftag)

        if "CELLAR" in st:
            cellar = parse_cellar(st["CELLAR"])
            txt.insert("end", "Mead Cellar", "sec")
            if cellar is None:
                txt.insert("end", "\n  Not built\n", "dim")
            else:
                txt.insert("end", f"  T{cellar['tier']}  "
                           f"[{cellar['stock']} / {cellar['cap']}]\n",
                           "dim")
                for qty, pct in cellar["brackets"]:
                    txt.insert("end", f"  {qty:>4} @ ", "num")
                    txt.insert("end", f"{pct}%\n", "cyan")

        refineries = parse_refinery(st.get("REFINERY", ""))
        if refineries:
            txt.insert("end", "Refineries\n", "sec")
            for r in refineries:
                self._row(txt, f"  {pretty_name(r['bldg'])} T{r['tier']}",
                          "val", f"{r['stock']}/{r['cap']}", "cyan")
                if r["grades"]:
                    txt.insert("end", "      " + "  ".join(
                        f"{g} {q} ({p}%)" for g, q, p in r["grades"])
                        + "\n", "dim")

        # PRODUCTION reuses the BUILDINGS 'good:amount,...' form.
        production = parse_buildings(st.get("PRODUCTION", ""))
        if production:
            txt.insert("end", "Production\n", "sec")
            for i in range(0, len(production), 2):
                line = ""
                for good, amt in production[i:i + 2]:
                    line += f"  {good:<14}{amt:>5}   "
                txt.insert("end", line.rstrip() + "\n", "val")

        if (st.get("BLOT") or "").strip():
            txt.insert("end", "Blot ", "sec")
            txt.insert("end", "(raw - unidentified)\n", "dim")
            txt.insert("end", "  " + st["BLOT"].strip() + "\n", "val")

        builds = parse_buildings(st.get("BUILDINGS", ""))
        items = [f"{pretty_name(n)} T{t}" for n, t in builds if t > 0]
        if items:
            txt.insert("end", "Buildings\n", "sec")
            for i in range(0, len(items), 2):
                left = "  " + items[i]
                right = items[i + 1] if i + 1 < len(items) else ""
                txt.insert("end", f"{left:<26}{right}\n", "val")

        if "MONUMENTS" in st:
            txt.insert("end", "Runic Monuments\n", "sec")
            mon = st["MONUMENTS"]
            try:
                count = int(mon)
            except ValueError:
                count = None
            if count is not None:
                txt.insert("end", f"  ({count}/5 slots)\n", "dim")
                if count == 0:
                    txt.insert("end", "  None inscribed\n", "dim")

        txt.configure(state="disabled")

    # -------------------------------------------------------- Trade tab
    def _render_trade(self, st):
        txt = self.trade_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")

        txt.insert("end", "Carts\n", "sec")
        carts = parse_carts(st.get("CARTS", ""))
        idle = split_entries(st.get("CIDLE", ""))
        if not carts and not idle:
            txt.insert("end", "  No carts\n", "dim")
        for c in carts:
            head = (f"  #{c['cart_id']} {c['mode']} {c['amt']} "
                    f"{c['good']} → {c['village']}")
            self._row(txt, head, "val", fmt_secs(c["secs"]), "cyan")
            extra = (f"      T{c['tier']}  q{c['quality']}%  "
                     f"dur{c['dur']}%  cap{c['cap']}")
            if c["escort"] not in ("", "0"):
                extra += f"  escort {c['escort']}"
            txt.insert("end", extra + "\n", "dim")
            for mode, good, amt, village in c["legs"]:
                txt.insert("end",
                           f"        ↳ {mode} {amt} {good} @ {village}\n",
                           "dim")
        for f in idle:                       # cart_id|tier|durability|cap
            txt.insert("end",
                       f"  #{field(f, 0)} idle   T{field(f, 1)}  "
                       f"dur{field(f, 2)}%  cap{field(f, 3)}\n", "dim")

        txt.insert("end", "Market Orders\n", "sec")
        if not split_entries(st.get("MARKET", "")):
            txt.insert("end", "  No open orders\n", "dim")

        txt.insert("end", "Incoming Fills\n", "sec")
        if not split_entries(st.get("INCOMING", "")):
            txt.insert("end", "  None incoming\n", "dim")

        routes = parse_routes(st.get("ROUTES", ""))
        if routes:
            txt.insert("end", "Routes\n", "sec")
            for r in routes:
                txt.insert("end", f"  {r['name']:<16}", "val")
                txt.insert("end", f"road T{r['road']} ", "dim")
                txt.insert("end", f"{r['road_maint']:>3}%",
                           stat_band(r["road_maint"]))
                txt.insert("end", f"   fort T{r['fort']} ", "dim")
                txt.insert("end", f"{r['fort_maint']:>3}%\n",
                           stat_band(r["fort_maint"]))

        rbuild = parse_rbuild(st.get("RBUILD", ""))
        if rbuild:
            txt.insert("end", "Roads/Forts Under Construction\n", "sec")
            for b in rbuild:
                if b["secs_left"] < 0:
                    status, tag = "awaiting materials", "res_no"
                elif b["secs_left"] == 0:
                    status, tag = "finalizing", "res_ok"
                else:
                    status, tag = fmt_secs(b["secs_left"]) + " left", "cyan"
                self._row(txt, f"  {b['name']} {b['kind']} → "
                          f"T{b['tier']}", "val", status, tag)
                if b["mats"]:
                    txt.insert("end", "    ", "dim")
                    for good, have, need in b["mats"]:
                        tag = "res_ok" if have >= need else "res_no"
                        txt.insert("end", f"{good} {have}/{need}   ", tag)
                    txt.insert("end", "\n")

        # Named in the 2026-07 doc with no documented format.
        for key in ("SUPG", "CUPG"):
            if (st.get(key) or "").strip():
                txt.insert("end", f"{key} ", "sec")
                txt.insert("end", "(raw - unidentified)\n", "dim")
                txt.insert("end", "  " + st[key].strip() + "\n", "val")

        txt.configure(state="disabled")

    # -------------------------------------------------------- Raids tab
    def _render_raids(self, st):
        txt = self.raids_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")

        if "SHIPS" in st:
            txt.insert("end", "Longships\n", "sec")
            ships = parse_ships(st["SHIPS"])
            if not ships:
                txt.insert("end", "  None\n", "dim")
            for s in ships:
                head = f"  {s['name']} T{s['tier']}  {s['state']}"
                if s["held"] == "1":
                    head += "  [held]"
                self._row(txt, head, "val", f"crew {s['crew']}", "cyan")
                if s["state"] in ("raiding", "voyaging", "building",
                                  "upgrading") and s["target"]:
                    suffix = (f"  ({fmt_secs(s['secs'])})"
                              if s["secs"] not in ("", "0") else "")
                    txt.insert("end", f"      → {s['target']}{suffix}\n",
                               "dim")
                detail = []
                if s["saga_title"]:
                    detail.append(f"{s['saga_title']} "
                                  f"({s['saga_raids']} raids)")
                if s["convoy"] == "1":
                    detail.append(f"convoy x{s['convoy_size']} "
                                  f"+{s['convoy_bonus']}")
                if detail:
                    txt.insert("end", "      " + " · ".join(detail)
                               + "\n", "dim")

        if "RTARGETS" in st:
            lineage, historical = parse_rtargets(st["RTARGETS"])
            for label, group in (("Lineage Targets", lineage),
                                 ("Historical Targets", historical)):
                if not group:
                    continue
                txt.insert("end", label + "\n", "sec")
                for name, g1, g2 in group:
                    self._row(txt, f"  {name}", "val",
                              f"{g1}, {g2}", "dim")

        if "RAIDLOG" in st:
            # The wire sends oldest first; show newest first.
            raidlog = parse_raidlog(st["RAIDLOG"])[::-1]
            txt.insert("end", "Raid Log ", "sec")
            txt.insert("end", "(newest first)\n", "dim")
            if not raidlog:
                txt.insert("end", "  No raids yet\n", "dim")
            for r in raidlog:
                head = f"  {r['ship']} → {r['target']}"
                if r["lost"]:
                    self._row(txt, head, "val", "SHIP LOST", "res_no")
                else:
                    self._row(txt, head, "val",
                              f"{r['daler']:,} daler", "gold")
                    loot = "  ".join(f"{g} {q}" for g, q in r["goods"])
                    parts = [p for p in
                             (f"{r['thralls']} thralls"
                              if r["thralls"] else "", loot) if p]
                    if parts:
                        txt.insert("end", "      " + " · ".join(parts)
                                   + "\n", "dim")

        txt.configure(state="disabled")

    # ------------------------------------------------------- People tab
    def _stat_bar(self, txt, label, pct):
        pct = max(0, min(100, pct))
        tag = stat_band(pct)
        txt.insert("end", f"  {label:<13}", "dim")
        txt.insert("end", fresh_bar(pct, 22), tag)
        txt.insert("end", f"{pct:>4}%\n", tag)

    def _render_people(self, st):
        txt = self.people_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")

        # --- Settlers (12 of 18 SETTLERX fields confirmed against live
        # `vsettler status`/`vsettler community` readouts - see the
        # SETTLERX comment in viking.py for the cross-reference; the rest
        # are still raw/unidentified) ---
        settlers = (st.get("SETTLERS", "") or "").split("|")
        x = (st.get("SETTLERX", "") or "").split("|")
        txt.insert("end", "Settlers\n", "sec")
        pop = field(settlers, 0, "0")
        txt.insert("end", "  Population: ", "dim")
        txt.insert("end", pop + "\n", "val")
        self._stat_bar(txt, "Mood",
                        _settlerx_int(settlers, SETTLERS_MOOD))
        for label, idx in SETTLERX_BARS:
            self._stat_bar(txt, label, _settlerx_int(x, idx))
        cap = _settlerx_int(x, SETTLERX_HOUSING_CAP)
        plots = _settlerx_int(x, SETTLERX_HOUSING_PLOTS)
        avg = _settlerx_int(x, SETTLERX_HOUSING_AVG)
        self._row(txt, "  Housing:", "dim",
                  f"cap {cap}, {plots} plots, avg T{avg / 100:.2f}", "val")
        shplots = parse_shplots(st.get("SHPLOTS", ""))
        if any(c for _t, c in shplots):
            self._row(txt, "  Plots:", "dim", ", ".join(
                f"{c} x T{t}" for t, c in shplots if c), "val")
        edict_left = _settlerx_int(x, SETTLERX_EDICT_REMAINING)
        if edict_left > 0:
            self._row(txt, "  Edict:", "dim",
                      fmt_secs(edict_left) + " left", "cyan")
        else:
            cooldown = _settlerx_int(x, SETTLERX_EDICT_COOLDOWN)
            status = (fmt_secs(cooldown) + " cooldown" if cooldown > 0
                      else "ready")
            self._row(txt, "  Edict:", "dim", status, "val")
        jobs = _settlerx_int(x, SETTLERX_JOBS)
        employed = _settlerx_int(x, SETTLERX_EMPLOYED)
        staffed = _settlerx_int(x, SETTLERX_MARKET_STAFFED)
        self._row(txt, "  Jobs:", "dim",
                  f"{employed}/{jobs} employed, {staffed} market-staffed",
                  "val")
        mult = _settlerx_int(x, SETTLERX_HAPPINESS_MULT)
        net = _settlerx_int(x, SETTLERX_COMMUNITY_NET)
        upkeep = _settlerx_int(x, SETTLERX_UPKEEP)
        self._row(txt, "  Economy:", "dim",
                  f"happiness x{mult / 100:.2f}, net {net:+d}/tick, "
                  f"upkeep {upkeep}/tick", "val")
        unknown = [v for i, v in enumerate(x)
                  if i not in SETTLERX_KNOWN_INDICES and i > 0]
        if unknown and any(v for v in unknown):
            txt.insert("end", "  Other fields ", "dim")
            txt.insert("end", "(raw - unidentified)\n", "dim")
            txt.insert("end", "  " + " | ".join(unknown) + "\n", "val")
        try:
            no_settlers = int(pop) == 0
        except ValueError:
            no_settlers = False
        if no_settlers:
            txt.insert("end", "  No settlers yet; housing and community "
                       "still update here.\n", "dim")

        # --- Civic buildings + settler upkeep (SCIVICS/SCONSUME) ---
        civics = parse_semi_pairs(st.get("SCIVICS", ""))
        if civics:
            txt.insert("end", "Civic Buildings\n", "sec")
            items = [f"{pretty_name(n)} T{v}" for n, v in civics]
            for i in range(0, len(items), 2):
                left = "  " + items[i]
                right = items[i + 1] if i + 1 < len(items) else ""
                txt.insert("end", f"{left:<26}{right}\n", "val")

        consume = parse_semi_pairs(st.get("SCONSUME", ""))
        if consume:
            txt.insert("end", "Settler Upkeep\n", "sec")
            txt.insert("end", "  " + "   ".join(
                f"{pretty_name(n)} {v}" for n, v in consume) + "\n",
                "val")

        # --- Settler community actions (SACTIONS) ---
        # Cracked 2026-06-20 (see parse_sactions) - a flat 6-number list,
        # one slot per action in SACTIONS_NAMES order, holding the
        # remaining seconds if that action is currently active. Only one
        # action can be active at a time, so show that one (or "Action
        # available" if none are) rather than all 6 slots.
        sactions = (st.get("SACTIONS", "") or "").strip()
        if sactions:
            txt.insert("end", "Settler Actions\n", "sec")
            active = next(((name, secs) for name, secs
                          in parse_sactions(sactions) if secs > 0), None)
            if active:
                name, secs = active
                self._row(txt, f"  {pretty_name(name)}:", "dim",
                          fmt_secs(secs) + " left", "cyan")
            else:
                txt.insert("end", "  Action available\n", "val")

        # --- Garrison (HIRD roster; soldier stat pips not yet mapped) ---
        txt.insert("end", "Garrison\n", "sec")
        hird = split_entries(st.get("HIRD", ""))
        if not hird:
            txt.insert("end", "  None\n", "dim")
        for f in hird:
            name = field(f, 1, "?")
            loc = pretty_name(field(f, 2, "")).replace("City ", "")
            age = field(f, 8, "").title()
            stance = field(f, 9, "").title()
            extra = " · ".join(p for p in (age, loc, stance) if p)
            txt.insert("end", f"  {name}", "val")
            txt.insert("end", f"   {extra}\n" if extra else "\n", "dim")

        # --- Patrol (hirdmadr shift) ---
        patrol = parse_patrol(st.get("PATROL", ""))
        if patrol is not None:
            count, secs = patrol
            txt.insert("end", "Patrol\n", "sec")
            if count:
                self._row(txt, f"  {count} hirdmadr on patrol", "val",
                          f"shift ends in {fmt_secs(secs)}", "cyan")
            else:
                txt.insert("end", "  None on patrol\n", "dim")

        # --- Staff (the vroster) ---
        staff = parse_staff(st.get("STAFF", ""))
        if staff:
            txt.insert("end", "Staff\n", "sec")
            for m in staff:
                extra = " · ".join(p for p in (
                    m["assigned"], m["specialty"], m["trait"]) if p)
                txt.insert("end", f"  {m['name']}", "val")
                if extra:
                    txt.insert("end", f"   {extra}", "dim")
                # Loyalty + age read as one phrase: 'Uneasy Veteran',
                # colored by the loyalty band.
                band = ("" if m["loyalty"] is None else
                        STAFF_LOYALTY.get(m["loyalty"],
                                          str(m["loyalty"])))
                band = " ".join(p for p in (band, m["age"].title())
                                if p)
                if band:
                    tag = ("dim" if m["loyalty"] is None else
                           "stat_hi" if m["loyalty"] >= 4 else
                           "stat_mid" if m["loyalty"] == 3 else
                           "stat_lo")
                    txt.insert("end", f"   {band}", tag)
                txt.insert("end", "\n")
                if m["stats"]:
                    line = "      " + "  ".join(
                        f"{n} {v}" for n, v in m["stats"])
                    if m["unknown2"] not in ("", "0"):
                        line += f"   [? {m['unknown2']}]"
                    txt.insert("end", line + "\n", "cyan")

        # --- Varangian Guards ---
        txt.insert("end", "Varangian Guards\n", "sec")
        if not (st.get("VARANG", "") or "").strip():
            txt.insert("end", "  None dispatched.\n", "dim")
            txt.insert("end", "  None received.\n", "dim")

        # --- Incoming Raids ---
        txt.insert("end", "Incoming Raids\n", "sec")
        raid = (st.get("RAID", "") or "").split("|")
        try:
            rsecs = int(field(raid, 0, "-1"))
        except ValueError:
            rsecs = -1
        if rsecs < 0:
            txt.insert("end", "  No raid currently scheduled.\n", "dim")
        else:
            self._row(txt, f"  {field(raid, 1, 'raiders')} raid", "val",
                      fmt_secs(rsecs), "cyan")

        # --- Missions ---
        txt.insert("end", "Missions\n", "sec")
        mission = st.get("MISSIONS", "")
        if not mission:
            txt.insert("end", "  No active missions\n", "dim")
        else:
            mf = mission.split("|")
            txt.insert("end", f"  {field(mf, 1, '?')}\n", "val")
            detail = " · ".join(p for p in (field(mf, 6, ""),
                                            field(mf, 7, "")) if p)
            if detail:
                txt.insert("end", f"    → {detail}\n", "dim")

        # --- BDMG: named in the guild help doc with no field format
        # documented and no capture yet, so shown raw until identified ---
        bdmg = (st.get("BDMG") or "").strip()
        if bdmg:
            txt.insert("end", "Building Damage ", "sec")
            txt.insert("end", "(raw - unidentified)\n", "dim")
            txt.insert("end", "  " + bdmg + "\n", "val")

        txt.configure(state="disabled")

    # ---------------------------------------------------------- Map tab
    def _render_map(self, st):
        cols, rows, px, py = parse_vmaph(st.get("VMAPH", ""))
        if cols <= 0 or rows <= 0:
            return
        grid = collect_vmr(st, rows)
        pois = parse_vmapl(st.get("VMAPL", ""))
        # Skip the heavy canvas redraw unless the map actually changed.
        sig = (st.get("VMAPH"), st.get("VMAPL"), tuple(grid))
        if sig == self._map_sig:
            return
        self._map_sig = sig

        marks = [(x, y, POI_COLOR.get(t, DEFAULT_POI_COLOR))
                 for t, _n, x, y in pois]
        self.map_canvas.set_data(grid, cols, rows, marks,
                                 (px, py) if px is not None else None)
        if px is not None:
            self.map_pos.configure(text=f"@{px},{py}")

        txt = self.map_poi
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        col_w = 30
        for i in range(0, len(pois), 2):
            for j in (i, i + 1):
                if j >= len(pois):
                    continue
                kind, name, x, y = pois[j]
                tag = "poi_" + kind if kind in POI_COLOR else "poi_default"
                cell = f"{POI_LABEL.get(kind, kind):<4} {name}"
                # Each entry is click-to-walk (same walk_cb as clicking
                # the map cell itself). One tag per list slot; re-binding
                # on re-render replaces the old lambda, so stale
                # coordinates never linger.
                link = f"poi_link_{j}"
                txt.insert("end", cell, (tag, link))
                txt.insert("end", " " * max(0, col_w - len(cell)))
                if self.walk_cb:
                    txt.tag_bind(link, "<Button-1>",
                                 lambda _e, wx=x, wy=y:
                                     self.walk_cb(wx, wy))
                    txt.tag_bind(link, "<Enter>",
                                 lambda _e: txt.configure(cursor="hand2"))
                    txt.tag_bind(link, "<Leave>",
                                 lambda _e: txt.configure(cursor=""))
            txt.insert("end", "\n")
        txt.configure(state="disabled")

    # --------------------------------------------------- Stats/Ranks/Bonds
    def _res_bar(self, txt, label, cur, mx):
        pct = min(100, int(100 * cur / mx)) if mx else 0
        txt.insert("end", f"  {label:<6}", "dim")
        txt.insert("end", fresh_bar(pct, 14), stat_band(pct))
        txt.insert("end", f"{cur:>6}/{mx}\n", "num")

    def set_vskills(self, data):
        """Store the latest parsed `vskills` (see parse_vskills) and re-render
        the Stats tab so the GXP skill costs show immediately."""
        self.vskills = data
        if self.stats_txt is not None:
            self._render_stats(self.state_data)

    def _pool_points(self, st, pool):
        """Current points for a GXP pool: the live mip_extra value if we have
        it (refreshes every combat round), else the value captured from the
        last `vskills` read."""
        key = POOL_MIP.get(pool)
        if key and key in st:
            try:
                return int(st[key])
            except (TypeError, ValueError):
                pass
        for tree in (self.vskills or {}).get("trees", []):
            if tree["pool"] == pool:
                return tree["points"]
        return 0

    def _render_gxp(self, txt, st):
        """The Guild Experience block: Daler total + each GXP pool (live from
        MIP) with its per-skill training costs from the last `vskills`."""
        txt.insert("end", "Guild Experience\n", "sec")
        daler = st.get("DALER")
        if daler is None and self.vskills:
            daler = self.vskills.get("daler")
        if daler is not None:
            try:
                txt.insert("end", f"  Daler: {int(daler):,}\n", "gold")
            except (TypeError, ValueError):
                pass
        if not self.vskills or not self.vskills.get("trees"):
            txt.insert("end",
                       "  Send `Vskills` to load skill costs.\n", "dim")
            return
        for tree in self.vskills["trees"]:
            pts = self._pool_points(st, tree["pool"])
            txt.insert("end", f"\n  {tree['pool']}: ", "sec")
            txt.insert("end", f"{pts:,} points\n", "num")
            for sk in tree["skills"]:
                ind = "      " if sk["child"] else "    "
                txt.insert("end", f"{ind}{sk['name']} ", "val")
                txt.insert("end", f"L{sk['level']}", "num")
                if sk["saga"] is None:
                    txt.insert("end", "   max\n", "gold")
                else:
                    cost = f"   {sk['saga']:,}"
                    if sk["daler"]:
                        cost += f" ({sk['daler']:,})"
                    txt.insert("end", cost + "\n", "cost")

    def _glvl_precise(self):
        """Guild level = sum of all skill levels / 4 (GAME FACT from the
        user, 2026-07-11), computed from the last `vskills` read. The
        fraction matters: 80.75 means one skill raise short of glvl 81.
        Returns a display string, or None if vskills isn't loaded."""
        if not self.vskills:
            return None
        total = sum(sk["level"] for tree in self.vskills.get("trees", [])
                    for sk in tree["skills"])
        if not total:
            return None
        return str(total // 4) if total % 4 == 0 else f"{total / 4:.2f}"

    def _render_stats(self, st):
        txt = self.stats_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        if st.get("LIN") or st.get("GLVL"):
            txt.insert("end", st.get("LIN", "?"), "sec")
            glvl = self._glvl_precise() or st.get("GLVL")
            if glvl:
                txt.insert("end", f"   Guild Level {glvl}", "dim")
            txt.insert("end", "\n")
        self._render_gxp(txt, st)
        txt.insert("end", "\n")
        txt.insert("end", "Resources ", "sec")
        txt.insert("end", "(abbrevs as the MUD sends them)\n", "dim")
        for name in STATS_RESOURCES:
            if name not in st:
                continue
            try:
                cur = int(st[name])
                mx = int(st.get("M" + name, "0"))
            except ValueError:
                continue
            self._res_bar(txt, name, cur, mx)
        if "FURY" in st:
            fill, total = parse_meter(st["FURY"])
            txt.insert("end", "Fury  ", "sec")
            txt.insert("end", "█" * fill + "░" * (total - fill), "fury")
            txt.insert("end", f"  {fill}/{total}\n", "num")
        fx = parse_effects(st.get("STFX", ""))
        if fx:
            txt.insert("end", "Effects\n", "sec")
            for nm, val in fx:
                txt.insert("end", f"  {nm} {val}\n", "val")
        enemy = st.get("ENN", "None")
        if enemy and enemy != "None":
            txt.insert("end", "Target\n", "sec")
            txt.insert("end", f"  {enemy}\n", "val")
        txt.configure(state="disabled")

    def _render_ranks(self, st):
        txt = self.ranks_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("end", "Reputation\n", "sec")
        # VREP semantics confirmed by the user 2026-07-11: rep (f2) is
        # the LIFETIME rep with that hold, f4/f5 the CUMULATIVE
        # thresholds of the current/next rank (525 got rank 2, 2025
        # total gets rank 3) - so within-rank progress is rep-f4 out of
        # f5-f4 (e.g. 1042/1500 for rep 1567).
        for _hid, name, rep, rank, cur, nxt in parse_vrep(
                st.get("VREP", "")):
            try:
                progress = f"({rep - int(cur):,}/{int(nxt) - int(cur):,})"
            except ValueError:
                progress = f"({rep:,}/{nxt})"
            self._row(txt, f"  {name:<16}rank {rank}", "val",
                      progress, "dim")
        txt.configure(state="disabled")

    def _render_bonds(self, st):
        txt = self.bonds_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        home = (st.get("LIN", "") or "").lower()
        txt.insert("end", "Standings\n", "sec")
        rows = parse_standings(st.get("STANDINGS", ""))
        if not rows:
            txt.insert("end", "  None\n", "dim")
        for _hid, name, val, label, flag in rows:
            tag = "st_" + label.lower() if label.lower() in \
                STANDING_COLOR else "val"
            txt.insert("end", f"  {name:<16}", "val")
            txt.insert("end", f"{label:<10}", tag)
            txt.insert("end", f"{val:>4}", "num")
            if flag == "1" or name.lower() == home:
                txt.insert("end", "  ★ home", "gold")
            txt.insert("end", "\n")
        bonds = parse_bonds(st.get("BONDS", ""))
        if bonds:
            names = hird_names(st)
            txt.insert("end", "Guard Bonds\n", "sec")
            for id1, id2, pts, lvl in bonds:
                pair = (f"{names.get(id1, '#' + id1)} + "
                        f"{names.get(id2, '#' + id2)}")
                tier = BOND_LEVELS.get(lvl, f"L{lvl}")
                self._row(txt, f"  {pair}", "val",
                          f"{tier} ({pts:,})", "cyan")
        txt.configure(state="disabled")

    def _render_builds(self, st):
        txt = self.builds_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")

        txt.insert("end", "Under Construction\n", "sec")
        con = parse_construction(st.get("BUILDS", ""))
        if not con:
            txt.insert("end", "  Nothing being built\n", "dim")
        else:
            self._row(txt, f"  {pretty_name(con['name'])} → "
                      f"T{con['tier']}", "val",
                      f"{fmt_secs(con['remaining'])} left", "cyan")
            done = (con["total"] - con["remaining"])
            pct = int(100 * done / con["total"]) if con["total"] else 0
            txt.insert("end", "    ", "dim")
            txt.insert("end", fresh_bar(pct, 14), stat_band(pct))
            txt.insert("end", f" {pct}%\n", "num")
            if con["resources"]:
                txt.insert("end", "    ", "dim")
                for good, have, need in con["resources"]:
                    tag = "res_ok" if have >= need else "res_no"
                    txt.insert("end", f"{good} {have}/{need}   ", tag)
                txt.insert("end", "\n")

        sproj = split_entries(st.get("SPROJ", ""))
        if sproj:
            txt.insert("end", "Settler Projects\n", "sec")
            for f in sproj:           # kind|target_tier|secs_remaining
                self._row(txt, f"  {pretty_name(field(f, 0))} → "
                          f"T{field(f, 1)}", "val",
                          f"{fmt_secs(field(f, 2, '0'))} left", "cyan")

        builds = parse_buildings(st.get("BUILDINGS", ""))
        items = [f"{pretty_name(n)} T{t}" for n, t in builds if t > 0]
        if items:
            txt.insert("end", "Buildings\n", "sec")
            for i in range(0, len(items), 2):
                left = "  " + items[i]
                right = items[i + 1] if i + 1 < len(items) else ""
                txt.insert("end", f"{left:<26}{right}\n", "val")

        if "MONUMENTS" in st:
            txt.insert("end", "Runic Monuments\n", "sec")
            try:
                count = int(st["MONUMENTS"])
            except ValueError:
                count = None
            if count is not None:
                txt.insert("end", f"  ({count}/5 slots)\n", "dim")
                if count == 0:
                    txt.insert("end", "  None inscribed\n", "dim")
        txt.configure(state="disabled")

    def _render_farm(self, st):
        txt = self.farm_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        weather_mod, plots = parse_farm(st.get("FARM", ""))
        txt.insert("end", "Mushroom Farm\n", "sec")
        if weather_mod is not None:
            self._row(txt, "  Weather modifier:", "dim",
                      str(weather_mod), "val")
        if not plots:
            txt.insert("end", "  No plots.\n", "dim")
        for p in plots:
            if p["wilt_left"] == 0:
                status, tag = "WILTED", "res_no"
            elif p["time_left"] == 0:
                status, tag = "READY", "res_ok"
            else:
                status, tag = fmt_secs(p["time_left"]), "cyan"
            extra = " (fertilized)" if p["fertilized"] else ""
            self._row(txt, f"  {p['coord']} {pretty_name(p['shroom_id'])}"
                      f"{extra}", "val", status, tag)
        txt.configure(state="disabled")

    # ------------------------------------------------------------- close
    def _closed(self):
        if self.on_close:
            self.on_close()
        self.destroy()
