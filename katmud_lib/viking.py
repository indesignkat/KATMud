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
mip_trade_goods (the TGOODS key) does arrive whole in its own packet.
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
GOOD_NAMES = {
    "f": "furs", "h": "fish", "m": "mead", "a": "amber",
    "r": "runestones", "s": "spoils", "t": "timber", "i": "iron",
    "g": "grain",
}
HOLD_NAMES = {
    0: "Midgard", 1: "Lodbrok's Hold", 2: "Eiriksson Hold",
    3: "Ui Imair Hold", 4: "Rurikid Hold", 5: "Harfagre Hold",
    6: "Yngling Hold", 7: "Skallagrim Hold", 8: "Stenkil Hold",
    9: "Sverker Hold", 10: "Eric's Hold", 11: "Munso Hold",
    12: "Skjoldung Hold", 13: "Sigurdsson Hold",
}


def parse_tgoods(value):
    """TGOODS payload ->
        [(hold_id, [(good, level, supply, demand), ...]), ...]
    in the MUD's order (which the reference UI preserves left-to-right,
    top-to-bottom). 'level' is the demand pressure: +3..-3, where + means
    high demand / good to sell and - means oversupply. Neutral (0) goods
    are omitted by the MUD. All parsing is defensive: a malformed hold or
    good entry is skipped, never raised."""
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
            if len(parts) != 4:
                continue
            letter, lvl_s, sup_s, dem_s = parts
            try:
                goods.append((GOOD_NAMES.get(letter, letter),
                              int(lvl_s), int(sup_s), int(dem_s)))
            except ValueError:
                continue
        holds.append((hid, goods))
    return holds


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
    """WSTOCK 'good|amount|freshness;...' -> {good: [(amount, fresh%),..]}.
    A good can hold several batches at different freshness."""
    out = {}
    for f in split_entries(value):
        if len(f) != 3:
            continue
        good, amt, fresh = f
        try:
            out.setdefault(good, []).append((int(amt), int(fresh)))
        except ValueError:
            continue
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


def warehouse_cap(state):
    """Inferred: warehouse capacity = building tier x 250 (T2 -> 500,
    which matches the reference screenshot). Returns None if unknown."""
    for name, tier in parse_buildings(state.get("BUILDINGS", "")):
        if name == "warehouse":
            return tier * 250
    return None


def stock_totals(state):
    """Live WSTOCK feed -> {good: total units}, collapsing freshness
    batches (Missionlist doesn't care which batch a unit comes from). A
    good at zero stock is absent from WSTOCK entirely - the MUD sends
    'good|' with no amount/freshness - so callers should treat a missing
    key as zero rather than as 'unknown'."""
    totals = {}
    for good, batches in parse_wstock(state.get("WSTOCK", "")).items():
        totals[good] = sum(amt for amt, _fresh in batches)
    return totals


_VTRADE_STOCK_RE = re.compile(
    r"^-~\*\s+([A-Za-z]+)\s+([\d,]+)\s+units\b", re.IGNORECASE)
_GOOD_SET = set(GOOD_NAMES.values())


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
        good = m.group(1).lower()
        if good not in _GOOD_SET:
            continue
        try:
            totals[good] = int(m.group(2).replace(",", ""))
        except ValueError:
            continue
    return totals


VTRADE_PRICE_RE = re.compile(
    r"^-~\*\s+([A-Za-z]+)\s+([\d,]+)\s+daler\b", re.IGNORECASE)


def parse_vtrade_prices(lines):
    """A captured `vtrade prices` readout -> {good: daler per unit}. Each
    good appears on one bordered line as '<Name>  <n> daler  [ min - max]';
    the header/border lines don't match the regex and are skipped."""
    prices = {}
    for line in lines:
        m = VTRADE_PRICE_RE.match(line)
        if not m:
            continue
        good = m.group(1).lower()
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
_MISSION_ID_RE = re.compile(r"\[(\d{3,6})\]")
_MISSION_GOOD_RE = re.compile(
    r"(\d+)\s+(" + "|".join(GOOD_NAMES.values()) + r")\b", re.IGNORECASE)
# Matches the 'Need: stock/required good' line added in newer vmission output.
# Format: 'Need: 52/13 grain, 8/5 timber' -> captures (13, 'grain'), (5, 'timber').
# Prefer this over prose-matching when present to avoid double-counting goods
# that also appear in the description text.
_MISSION_NEED_RE = re.compile(
    r"\d+/(\d+)\s+(" + "|".join(GOOD_NAMES.values()) + r")\b", re.IGNORECASE)
_MISSION_REWARD_RE = re.compile(
    r"Reward:\s*(\d+)\s*rep\s*\+\s*([\d,]+)\s*daler", re.IGNORECASE)
_MISSION_TOWN_RE = re.compile(r"\[\d+\]\s*([A-Za-z'\s]+?)\s*[:(]")
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
        reqs = [(int(q), g.lower())
                for q, g in _MISSION_NEED_RE.findall(chunk)]
        if not reqs:
            reqs = [(int(q), g.lower())
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
# so the acceptable metrics are daler plus the 7 goods actually seen there.
NEWBIE_METRICS = ("daler", "timber", "iron", "furs", "fish", "grain",
                  "mead", "amber")


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
        goods = [(int(q), g.lower())
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


CITY_KEYS = ("DALER", "GOD_POWER", "SHIPS", "CARTS", "MARKET",
             "INCOMING", "WSTOCK", "BUILDINGS", "MONUMENTS")

PEOPLE_KEYS = ("SETTLERS", "SETTLERX", "HIRD", "VARANG", "RAID",
               "MISSIONS", "ERRAND", "GARRISON", "SACTIONS")

FARM_KEYS = ("FARM",)


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


def stat_band(pct):
    if pct < 34:
        return "stat_lo"
    if pct < 67:
        return "stat_mid"
    return "stat_hi"


# ======================================================================
# Status window
# ======================================================================
# Tab layout mirrors the reference client (two rows of five). Only Goods
# is wired up so far; the rest name the feed that will power them, so the
# window doubles as the roadmap for the remaining Viking MIP work.
TABS = [["Stats", "City", "Farm", "Builds", "People"],
        ["Goods", "Map", "Bonds", "Ranks", "Sea"]]
TAB_FEED = {
    "Stats": "mip_extra", "City": "mip_city", "Farm": "mip_city",
    "Builds": "mip_city", "People": "mip_city",
    "Goods": "mip_trade_goods", "Map": "mip_map", "Bonds": "mip_city",
    "Ranks": "mip_city", "Sea": "mip_voyage",
}

LEVEL_SYM = {3: "+++", 2: "++", 1: "+", 0: "",
             -1: "-", -2: "--", -3: "---"}
LEVEL_COLOR = {3: "#5fd65f", 2: "#7cc869", 1: "#9cbf77", 0: "#888888",
               -1: "#bf9b77", -2: "#cc7a52", -3: "#d65151"}
GOOD_COLOR = {
    "furs": "#d2a679", "fish": "#6bb5c9", "mead": "#d98c8c",
    "amber": "#e0a82e", "runestones": "#9aa3ad", "spoils": "#c79154",
    "timber": "#a8895f", "iron": "#9fb2c2", "grain": "#c9c45f",
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

POI_LABEL = {"capital": "Cap", "lineage": "Lin", "settlement": "Set",
             "mentor_jarl": "Jarl"}
POI_COLOR = {"capital": "#e8902e", "lineage": "#d8704a",
             "settlement": "#e0d040", "mentor_jarl": "#c065c0"}
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


def map_landmarks(state, custom=None):
    """name(lowercased) -> (x, y, label). POIs from VMAPL plus the user's
    custom marks (custom: {name: [x, y]})."""
    out = {}
    for t, name, x, y in parse_vmapl(state.get("VMAPL", "")):
        out[name.lower()] = (x, y, POI_LABEL.get(t, t))
    for name, xy in (custom or {}).items():
        try:
            out[name.lower()] = (int(xy[0]), int(xy[1]), "mark")
        except (ValueError, IndexError, TypeError):
            continue
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
    r"(\+\s*)?([A-Za-z]+)\s*\.{2,}\s*\[\s*(\d+)\s*\(\s*([\d,]+)\s*\)\]"
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
    [{"name", "level", "saga", "daler", "child"}]}]}. Decorative borders and
    separators are ignored; partial/garbled lines that don't match are
    skipped, so it's safe to feed the raw capture."""
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
            cur["skills"].append({
                "name": m.group(2), "level": int(m.group(3)),
                "saga": _vsk_num(m.group(4)), "daler": _vsk_num(m.group(5)),
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
# VSAGA/VMEM. The fleet roster (LONGSHIP/SHIPS) already shows on the City
# tab, so the Sea tab focuses on the active voyage and its chart.
VOYAGE_KEYS = ("VOYAGE", "VCHH", "VQPATH", "VSAGA", "VMEM", "LONGSHIP")


def parse_voyage(value):
    """VOYAGE pipe-record -> labelled dict. The confident fields are locked
    against the reference screenshot; STRESS/RENOWN/PRESSURE/WEATHER indices
    are PROVISIONAL (inferred from which fields moved between two captures)
    and to be confirmed against more voyages."""
    f = value.split("|")
    g = lambda i: f[i].strip() if 0 <= i < len(f) else ""
    return {
        "state": g(0), "ship": g(2), "contract": g(3), "type": g(4),
        "danger": g(5), "col": g(6), "row": g(7),
        "grid_w": g(8), "grid_h": g(9),
        "hull": g(10), "morale": g(11), "supplies": g(12),
        "stress": g(13), "crew": g(14), "crew_max": g(15),
        "renown": g(16), "next": g(17),
        "threat": g(18), "threat_lvl": g(19), "pressure": g(20),
        "weather": g(21), "captain": g(24), "identity": g(25),
        "crew_desc": g(26), "traits": g(27),
    }


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
    """VQPATH 'c,r,c,r,...' -> [(col, row), ...] waypoints (queued route)."""
    nums = [n for n in value.split(",")
            if n.strip().lstrip("-").isdigit()]
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
        txt = self._scrolled_text(frame)
        txt.tag_configure("hold", foreground="#cdb87a",
                          font=self.mono_bold)
        txt.tag_configure("cycle", foreground="#6fb6d6")
        for good, col in GOOD_COLOR.items():
            txt.tag_configure("good_" + good, foreground=col)
        for lvl, col in LEVEL_COLOR.items():
            txt.tag_configure("lvl%d" % lvl, foreground=col)
        self.goods_txt = txt

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
             ("Pressure", v["pressure"]), ("Captain", v["captain"])])

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
        txt.configure(state="disabled")

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
            for i in range(0, len(goods), 2):
                txt.insert("end", "  ")
                self._cell(txt, goods[i])
                if i + 1 < len(goods):
                    txt.insert("end", "    ")
                    self._cell(txt, goods[i + 1])
                txt.insert("end", "\n")
            txt.insert("end", "\n")
        txt.configure(state="disabled")

    def _cell(self, txt, good_tuple):
        good, level, sup, dem = good_tuple
        gtag = "good_" + good
        if gtag not in txt.tag_names():
            gtag = "num"
        txt.insert("end", f"{good:<11}", gtag)
        txt.insert("end", f"{LEVEL_SYM.get(level, ''):>3}",
                   "lvl%d" % level if level in LEVEL_COLOR else "num")
        txt.insert("end", " Sup:", "dim")
        txt.insert("end", f"{sup:>3}", "num")
        txt.insert("end", " Dem:", "dim")
        txt.insert("end", f"{dem:>3}", "num")

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

        if "SHIPS" in st:
            txt.insert("end", "Longships\n", "sec")
            ships = split_entries(st["SHIPS"])
            if not ships:
                txt.insert("end", "  None\n", "dim")
            for f in ships:
                # name|tier|status|target|secs|load|cap (load/cap and the
                # target/eta positions are best-effort - flagged)
                name, status = field(f, 0), field(f, 2, "")
                self._row(txt, f"  {name}  {status}", "val",
                          f"{field(f, 5)}/{field(f, 6)}", "cyan")
                target, eta = field(f, 3, ""), field(f, 4, "")
                if status in ("voyaging", "raiding") and target:
                    suffix = f"  ({fmt_secs(eta)})" if eta not in \
                        ("", "0") else ""
                    txt.insert("end", f"      → {target}{suffix}\n",
                               "dim")

        txt.insert("end", "Carts\n", "sec")
        carts = split_entries(st.get("CARTS", ""))
        idle = split_entries(st.get("CIDLE", ""))
        if not carts and not idle:
            txt.insert("end", "  No carts\n", "dim")
        for f in carts:
            # mode|good|village|secs|amt|half_in|quality|id|tier|dur|cap
            # (validated against a live 'buy timber' cart)
            head = (f"  #{field(f, 7)} {field(f, 0)} {field(f, 4)} "
                    f"{field(f, 1)} → {field(f, 2)}")
            self._row(txt, head, "val", fmt_secs(field(f, 3, "")), "cyan")
            txt.insert("end",
                       f"      T{field(f, 8)}  q{field(f, 6)}%  "
                       f"dur{field(f, 9)}%  cap{field(f, 10)}\n", "dim")
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

        if "WSTOCK" in st:
            stock = parse_wstock(st["WSTOCK"])
            total = sum(a for b in stock.values() for a, _ in b)
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
                for i, (amt, fresh) in enumerate(
                        sorted(batches, key=lambda b: -b[1])):
                    band_i = next(j for j, (lo, _l, _c)
                                  in enumerate(FRESH_BANDS) if fresh >= lo)
                    label = FRESH_BANDS[band_i][1]
                    ftag = "fr%d" % band_i
                    txt.insert("end", f"  {good if i == 0 else '':<9}",
                               gtag)
                    txt.insert("end", f"{label:<11}", ftag)
                    txt.insert("end", f"{amt:>3} ", "num")
                    txt.insert("end", fresh_bar(fresh), ftag)
                    txt.insert("end", f"{fresh:>4}%\n", ftag)

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
                kind, name, _x, _y = pois[j]
                tag = "poi_" + kind if kind in POI_COLOR else "poi_default"
                cell = f"{POI_LABEL.get(kind, kind):<4} {name}"
                txt.insert("end", f"{cell:<{col_w}}", tag)
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
                txt.insert("end", f"{ind}{sk['name']}: ", "val")
                txt.insert("end",
                           f"{sk['saga']:,} ({sk['daler']:,})\n", "cost")

    def _render_stats(self, st):
        txt = self.stats_txt
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        if st.get("LIN") or st.get("GLVL"):
            txt.insert("end", st.get("LIN", "?"), "sec")
            if st.get("GLVL"):
                txt.insert("end", f"   Guild Level {st['GLVL']}", "dim")
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
        txt.insert("end", "Reputation ", "sec")
        txt.insert("end", "(rank/progress fields best-effort)\n", "dim")
        for _hid, name, rep, rank, cur, nxt in parse_vrep(
                st.get("VREP", "")):
            self._row(txt, f"  {name:<16}{rep:>5}", "val",
                      f"rank {rank} ({cur}/{nxt})", "dim")
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
        bonds = st.get("BONDS", "")
        if bonds:
            txt.insert("end", "Bonds\n", "sec")
            for f in split_entries(bonds):
                txt.insert("end", "  " + " | ".join(f) + "\n", "val")
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
