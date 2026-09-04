"""
katmud_scripts.py - user hooks for KatMUD. #reload to refresh.

Hooks: on_connect(client) | on_line(client, clean) |
       on_command(client, cmd) | on_mip(client, tag, data) |
       on_vitals(client, v) | on_tick(client)

Useful state: client.vars (dict, also via #var), client.rounds,
client.in_combat, client.vitals, client.vitals_max, client.room,
client.room_vnum, client.send_line(cmd), client.process_input(cmd),
client.write_local(txt).

CONTENTS
  1. Shadow-portal automation. ONE portal at a time; psummon spends
     one of a per-reset allowance (the P:n/m readout). The script
     reads P:n/m wherever it appears in output and refuses to summon
     at 0 remaining or while a portal is already open.
     TROUBLESHOOTING: #var psdebug = 1 prints WHY each rule did or
     didn't fire (rate-limited).
  2. Bot framework. Define paths in BOTS below, then:
        #var bot = train      start
        #var bot = off        stop (also: 0, or empty)
     The bot skips any room containing an online player (roster from
     'players 3s' / 'players local') OR anything in SKIP_ALWAYS -
     disguises, retitled shorts, and the like.
"""

import re
import time

# ---------------------------------------------------------- portals
PORTAL_OUT = "steps through the shadow portal."
PORTAL_BACK = "steps back into the shadow portal."
# The MUD's refusal when a portal is already open. SUBSTRING match -
# verify the exact wording with #log and trim this if it differs.
PORTAL_DENIED = "already have a portal"
PSUMMON_COOLDOWN = 30          # seconds between automatic psummons
PORTAL_LIFETIME = 120          # assume the portal gone after this long
PSUMMON_FRESH_PCT = 90         # rule 1: enemy above this = worth it
PSUMMON_LONG_PCT = 40          # rule 2: long fight, enemy above this

# P:5/8 style readout: 5 psummon uses left of 8 this reset. Parsed
# from ANY output line it appears in (hpbar, score, etc.).
_PORTAL_USES_RE = re.compile(r"\bP:\s*(\d+)\s*/\s*(\d+)")


def _portal_guild(client):
    """Shadow portals / psummon are a 3s-BLADESINGER mechanic only. Without
    this gate the auto-summon below ran for EVERY character/guild (it spammed
    'psummon' at mobs that have no portals), because on_vitals is global."""
    return (str(getattr(client, "mud", "")).lower() == "3s"
            and str(getattr(client, "guild", "")).lower() == "bladesingers")


def _portal_open(client):
    client.vars["portals_active"] = 1
    client.vars["_portals_t"] = time.time()


def _portal_closed(client):
    client.vars["portals_active"] = 0
    client.vars["portal_count"] = 0


def _psdebug(client, msg):
    if not client.vars.get("psdebug"):
        return
    now = time.time()
    if now - client.vars.get("_psdebug_t", 0) < 5:
        return
    client.vars["_psdebug_t"] = now
    client.write_local(f"[psdebug] {msg}", "#888899")


def on_command(client, cmd):
    if _sf2_command(client, cmd):
        return False                      # ours - never send it to the mud
    # Manual psummon also marks the portal open (optimistically; the
    # arrival lines confirm, the denial line corrects).
    if _portal_guild(client) and cmd.strip().startswith("psummon"):
        _portal_open(client)
    return None


def _portal_active(client):
    """Open flag with a lifetime fallback: if we never saw the copies
    leave (different text, left the room, log scrolled), stop
    believing in a ghost portal after PORTAL_LIFETIME seconds."""
    if not client.vars.get("portals_active", 0):
        return False
    if time.time() - client.vars.get("_portals_t", 0) > PORTAL_LIFETIME:
        _portal_closed(client)
        client.write_local("[portal assumed expired (lifetime)]",
                           "#aa88cc")
        return False
    return True


def _try_psummon(client, reason):
    now = time.time()
    if now - client.vars.get("_psummon_t", 0) < PSUMMON_COOLDOWN:
        _psdebug(client, "blocked: cooldown")
        return
    uses = client.vars.get("psummon_left")
    if uses is not None and int(uses) <= 0:
        _psdebug(client, "blocked: 0 psummon uses left this reset")
        return
    if not client.vars.get("portals_available", 1):
        _psdebug(client, "blocked: portals_available = 0")
        return
    client.vars["_psummon_t"] = now
    client.write_local(f"[auto-psummon: {reason}]", "#aa88cc")
    client.send_line("psummon")     # deadman-respecting: NOT manual
    if client.deadman_tripped:
        client.write_local("[psummon NOT sent: deadman tripped]",
                           "#ff5555")
    _portal_open(client)


def _age_guild(client):
    """`evoke age` is a 3s ELEMENTAL ability. on_vitals is global, so without
    this gate it would be sent by every character of every guild - the same
    bug the portal gate below exists to prevent."""
    return (str(getattr(client, "mud", "")).lower() == "3s"
            and str(getattr(client, "guild", "")).lower() == "elementals")


def evoke_age_rearm(client, _v=None):
    """Fight over -> arm the once-per-fight evoke again.

    A client hook, called from the GMCP Char.Combat empty snapshot - the
    one explicit end-of-fight marker MIP never had.

    GAME FACT (user, 2026-09-03): `evoke age` costs 1,000 LINK ENERGY -
    against a balance of 162 MILLION, so a second cast in one fight is
    effectively spam rather than waste. That inverts the usual caution: the
    failure worth avoiding is MISSING a fight, not repeating one. Hence the
    enemy-change re-arm below, which would be reckless for a genuinely
    expensive ability.

    Note link energy is the elementals' LETHAL-at-0 pool (see
    elemental.info_lines), so this is cheap, not free. Deliberately NOT
    gated on a link-energy floor: at the observed balance that guard would
    never fire, and GMCP Guild.Info carries the live figure if it ever
    needs adding.
    """
    client.vars["_age_armed"] = True


def _try_evoke_age(client):
    """ONE `evoke age` per fight, as early in it as possible.

    Hung off in_combat rather than "did I attack", so it covers a fight you
    did not start and one you are not tanking - in_combat is set from the
    FFF enemy field either way.

    ONE attempt, success or not (user's choice, 2026-09-03): a failed cast
    is lost for that fight rather than retried. That needs no success signal
    to stop it and so cannot spam, which a retry loop could if the mob
    simply cannot be aged.
    """
    if not _age_guild(client) or not client.evoke_age_auto:
        return
    if not client.in_combat:
        evoke_age_rearm(client)
        return
    # A DIFFERENT enemy means a different fight, even if in_combat never
    # dropped in between - back-to-back aggro would otherwise leave the
    # second fight un-aged. Safe to be eager here only because a redundant
    # cast costs 1,000 link energy out of millions; see evoke_age_rearm.
    enemy = str((client.vitals or {}).get("enemy") or "")
    if enemy and enemy != client.vars.get("_age_enemy"):
        client.vars["_age_enemy"] = enemy
        evoke_age_rearm(client)
    if not client.vars.get("_age_armed", True):
        return
    client.vars["_age_armed"] = False
    client.write_local("[auto: evoke age]", "#aa88cc")
    client.send_line("evoke age")   # deadman-respecting: NOT manual


def on_vitals(client, v):
    _try_evoke_age(client)

    if not _portal_guild(client):
        return                            # not a 3s bladesinger: no portals
    if not client.in_combat:
        return
    active = _portal_active(client)
    cond = v.get("enemycond", 100)    # 100 = not reported yet this fight
    _psdebug(client, f"rounds={client.rounds} cond={cond}% "
                     f"portal={'open' if active else 'closed'} "
                     f"copies={client.vars.get('portal_count', 0)} "
                     f"uses={client.vars.get('psummon_left', '?')}")
    # Rule 1: round 5+, enemy barely hurt, no portal open -> summon.
    if (client.rounds >= 5 and not active
            and cond > PSUMMON_FRESH_PCT):
        _try_psummon(client, f"round {client.rounds}, fresh enemy "
                             f"({cond}%)")
    # Rule 2: long fight, enemy still healthy, portal expired -> again.
    elif (client.rounds > 20 and not active
            and cond > PSUMMON_LONG_PCT):
        _try_psummon(client, f"long fight, enemy at {cond}%")


# ---------------------------------------------------------- botting
#
# Area bots: give a PATH to walk and the NAMES of killable mobs.
# In each room the bot:
#   1. waits a beat for the room text to arrive,
#   2. SKIPS the room if an online player is standing in it, or if
#      any SKIP_ALWAYS phrase appears in the room,
#   3. kills mobs matching your keywords (verified: if combat does not
#      start within a few seconds, the mob was not there - move on),
#   4. re-looks after each kill for more matches, then walks on.
#
# Players are recognized by name. The roster comes from the local
# player list ('players 3s' on port 3200, 'players local' on 3000 -
# plain 'who' on either mud lists BOTH muds and would skip for people
# a whole game away). Refreshed every WHO_REFRESH seconds, so someone
# logging in mid-lap is caught on the next refresh.
#
# SKIP_ALWAYS catches what the roster can't: guild disguises and
# retitle gear. Case-insensitive substring match against every line
# of room text. Add your own.
#
#   #var bot = recruits     start        #var bot = off    stop

SKIP_ALWAYS = [
    "big black wolf",            # disguised guild member
    "dread pirate roberts",      # retitle gear
    "avatar of lucanus",         # retitle gear
]

BOTS = {
    "recruits": {
        "path": ("e s e e e e e n w w w w w n e e e e e n w w w sw w s w").split(),   # the loop, one move per entry
        "kill": ["recruit"],            # mob keywords: kill <keyword>
        "loop": True,        # restart the path when it finishes
        "delay": 2.0,        # seconds between moves
        "stop_pct": 35,      # stop the bot below this hp% (0 = never)
        "stop_cmd": "",      # command on low-hp stop, e.g. "go camp"
        "max_kills": 8,      # per room, guards against re-look loops
        "after_kill": "get plate",    # optional command sent when a fight ends
        "skip": [],          # extra skip phrases, merged with SKIP_ALWAYS
        "resume_on": "Fully refreshed you end your revalrie.",
                             # hold after a kill until this text
                             # arrives ("" = don't wait). VERIFY the
                             # exact wording against a #log first.
        "resume_timeout": 90,   # give up waiting after this many secs
    },
}

SCAN_TIME = 1.5        # seconds to let room text arrive before deciding
KILL_TIMEOUT = 5.0     # combat must start within this, else no mob
WHO_REFRESH = 120      # seconds between roster refreshes
WHO_CAPTURE = 3.0      # seconds of output treated as roster after asking

# Words that can start a roster or room line but are never players.
_NOT_NAMES = {
    "the", "a", "an", "you", "there", "this", "that", "it", "two",
    "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "total", "totals", "players", "welcome", "name", "level", "idle",
    "guild", "obvious", "exits", "north", "south", "east", "west",
    "up", "down", "wiz", "avg",
}

_bot = {
    "name": "", "i": 0, "state": "idle", "until": 0.0,
    "lines": [], "kills": 0, "regen_done": False,
    "players": set(), "who_t": 0.0, "who_until": 0.0,
}


def _bot_stop(client, msg, color="#aa88cc"):
    client.vars["bot"] = "off"
    _bot.update(name="", i=0, state="idle", lines=[], kills=0)
    client.write_local(f"[bot: {msg}]", color)


def _hp_pct(client):
    hpmax = max(client.vitals.get("hpmax", 0),
                client.vitals_max.get("hp", 0))
    if not hpmax:
        return None
    return client.vitals.get("hp", 0) * 100.0 / hpmax


def _bot_active(client):
    return _bot["name"] and str(client.vars.get("bot", "")).strip() \
        .lower() == _bot["name"]


def _players_cmd(client):
    """Local-player list per mud. Plain 'who' shows BOTH 3k and 3s."""
    port = int(client.port or 0)
    if port == 3200:
        return "players 3s"
    if port == 3000:
        return "players local"
    return "who"


def _refresh_who(client, now):
    _bot["who_t"] = now
    _bot["who_until"] = now + WHO_CAPTURE
    client.send_line(_players_cmd(client))


def _capture_who_line(client, text):
    words = text.split()
    if not words:
        return
    # 'players' output prefixes some names with '-' (e.g. -Griddlorg)
    name = words[0].strip(".,:;!?()[]-")
    if (3 <= len(name) <= 15 and name[0].isupper() and name.isalpha()
            and name.lower() not in _NOT_NAMES):
        me = str(client.character or "").lower()
        if name.lower() != me:
            _bot["players"].add(name.lower())


def _occupant_here(client, bot):
    """Return a reason string if the room must be skipped: a roster
    name starts a line, or a skip phrase appears anywhere."""
    skips = [s.lower() for s in SKIP_ALWAYS + list(bot.get("skip", []))]
    for line in _bot["lines"]:
        low = line.lower()
        for phrase in skips:
            if phrase in low:
                return f"'{phrase}'"
        words = line.split()
        if not words:
            continue
        first = words[0].strip(".,:;!?()[]-").lower()
        if first in _bot["players"]:
            return words[0]
    return None


def _bot_send_move(client, bot, now):
    if _bot["i"] >= len(bot["path"]):
        if bot.get("loop"):
            _bot["i"] = 0
        else:
            _bot_stop(client, "path complete")
            return
    move = bot["path"][_bot["i"]]
    _bot["i"] += 1
    _bot["kills"] = 0
    _bot["lines"] = []
    _bot["state"] = "scan"
    _bot["until"] = now + max(bot.get("delay", 2.0), SCAN_TIME)
    client.process_input(move)


def _bot_evaluate_room(client, bot, now):
    reason = _occupant_here(client, bot)
    if reason:
        client.write_local(f"[bot: {reason} is here - moving on]",
                           "#ffaa55")
        _bot_send_move(client, bot, now)
        return
    if _bot["kills"] < bot.get("max_kills", 8):
        low = [ln.lower() for ln in _bot["lines"]
               if "corpse" not in ln.lower()
               and "remains" not in ln.lower()]
        for kw in bot.get("kill", []):
            if any(kw.lower() in ln for ln in low):
                _bot["kills"] += 1
                _bot["lines"] = []
                _bot["regen_done"] = False
                _bot["state"] = "kill_wait"
                _bot["until"] = now + KILL_TIMEOUT
                client.send_line(f"kill {kw}")
                return
    _bot_send_move(client, bot, now)


def on_line(client, text):
    _sf2_line(client, text)
    # Portal-use allowance: read P:n/m wherever it shows up.
    m = _PORTAL_USES_RE.search(text)
    if m:
        client.vars["psummon_left"] = int(m.group(1))
        client.vars["psummon_max"] = int(m.group(2))
    # Bot: collect room text while scanning; harvest roster names.
    if _bot["name"]:
        now = time.time()
        if now < _bot["who_until"]:
            _capture_who_line(client, text)
        elif _bot["state"] == "scan":
            _bot["lines"].append(text)
        if _bot["state"] in ("fight", "regen"):
            bot = BOTS.get(_bot["name"], {})
            resume = bot.get("resume_on", "")
            if resume and resume in text:
                _bot["regen_done"] = True
    # One portal at a time. Arrivals/departures of the copies bracket
    # its life; the denial line corrects an optimistic guess.
    if PORTAL_OUT in text:
        client.vars["portal_count"] = client.vars.get("portal_count", 0) + 1
        _portal_open(client)
    elif PORTAL_BACK in text:
        n = client.vars.get("portal_count", 0) - 1
        client.vars["portal_count"] = max(0, n)
        if client.vars["portal_count"] == 0:
            _portal_closed(client)
            client.write_local("[portal closed]", "#aa88cc")
    elif PORTAL_DENIED in text:
        # We summoned into an already-open portal: keep it marked
        # open and refresh the lifetime clock so rule 2 waits.
        _portal_open(client)
        _psdebug(client, "MUD says portal already open")


# ------------------------------------------------- Street Fighter II
#
# The SFII arcade (3s) is a menu-driven gauntlet, not a place you walk:
#
#     enter cabinet -> select <difficulty> -> choose <fighter>
#     then per opponent:  enter <name> -> challenge <name> -> kill <name>
#     ...and you are teleported back to the hub when they die.
#
# Run it with     sf2 hard guile            (difficulty, then fighter)
#                 sf2 deadly ryu
# Stop it with    sf2 off
# Status          sf2
#
# `progress` is the ONLY source of truth. The script never remembers who it
# has beaten, so the area's mid-run reset bug needs no detection: the names
# simply reappear as [ ] and get fought again. That also means a fight that
# somehow does not register just gets repeated rather than skipped.
SF2_PROGRESS_WINDOW = 2.0     # seconds to collect the `progress` reply
SF2_FIGHT_TIMEOUT = 180.0     # give up on one opponent after this long
SF2_STEP_DELAY = 2.0          # one 3s combat round between sends

# `progress` lists an opponent as "  [X] CHUN-LI" (beaten) or "  [ ] KEN".
SF2_ENTRY_RE = re.compile(r"^\s*\[([ Xx])\]\s*(\S.*?)\s*$")
SF2_HEADER = "=== TOURNAMENT PROGRESS ==="

# Only M. Bison's name is inconsistent: `progress` prints him as M_BISON in
# one section and "M. BISON - CHAMPION!" in the other, while the commands
# want `enter m-bison` and `challenge m bison`. Rather than try to match
# every spelling, just look for "bison" anywhere in the listed name.
# Everyone else works exactly as listed, lowercased (chun-li, e-honda, ...).
SF2_ALIASES = (("bison", ("m-bison", "m bison")),)

_sf2 = {"on": False, "state": "idle", "difficulty": "", "fighter": "",
        "target": "", "until": 0.0, "lines": [], "last_target": "",
        "repeats": 0, "deadline": 0.0, "hub": None}


def _sf2_names(listed):
    """progress name -> (enter name, challenge name)."""
    low = listed.strip().lower()
    for needle, cmds in SF2_ALIASES:
        if needle in low:
            return cmds
    return low, low


def _sf2_stop(client, why, colour="#aa88cc"):
    _sf2.update(on=False, state="idle", target="", lines=[])
    client.vars["sf2"] = "off"
    client.write_local("[sf2] " + why, colour)


def _sf2_line(client, text):
    """Collect the `progress` reply while the read window is open."""
    if not _sf2["on"] or _sf2["state"] != "read":
        return
    if SF2_HEADER in text:
        _sf2["lines"] = []
        return
    m = SF2_ENTRY_RE.match(text)
    if m:
        _sf2["lines"].append((m.group(1).strip() == "", m.group(2)))


def _sf2_pending(entries):
    """First opponent still to beat, or None. `entries` is [(pending, name)].
    Order is the mud's own: world warriors first, then each boss as it
    unlocks, so 'first pending' is always the right next fight."""
    for pending, name in entries:
        if pending:
            return name
    return None


def _sf2_command(client, cmd):
    """`sf2 <difficulty> <fighter>` | `sf2 off` | `sf2`. Returns True if this
    was ours, so on_command can swallow it instead of sending it to the mud."""
    parts = cmd.strip().split()
    if not parts or parts[0].lower() != "sf2":
        return False
    args = [a.lower() for a in parts[1:]]
    if not args:
        if _sf2["on"]:
            client.write_local(
                "[sf2] running: %s / %s - state %s%s"
                % (_sf2["difficulty"], _sf2["fighter"], _sf2["state"],
                   (", on " + _sf2["target"]) if _sf2["target"] else ""),
                "#aa88cc")
        else:
            client.write_local("[sf2] not running. Usage: "
                               "sf2 <difficulty> <fighter> | sf2 off",
                               "#aa88cc")
        return True
    if args[0] in ("off", "stop", "0"):
        if _sf2["on"]:
            _sf2_stop(client, "stopped")
        else:
            client.vars["sf2"] = "off"
            client.write_local("[sf2] not running.", "#aa88cc")
        return True
    if len(args) != 2:
        client.write_local("[sf2] usage: sf2 <difficulty> <fighter>, "
                           "e.g. 'sf2 hard guile'", "#ff5555")
        return True
    client.vars["sf2"] = " ".join(args)
    return True


def _sf2_tick(client):
    name = str(client.vars.get("sf2", "") or "").strip().lower()
    if name in ("", "0", "off"):
        if _sf2["on"]:
            _sf2.update(on=False, state="idle", target="", lines=[])
        return
    now = time.time()

    if not _sf2["on"]:
        parts = name.split()
        if len(parts) != 2:
            _sf2_stop(client, "usage: #var sf2 = <difficulty> <fighter>, "
                              "e.g. 'hard guile'", "#ff5555")
            return
        _sf2.update(on=True, state="init", difficulty=parts[0],
                    fighter=parts[1], target="", lines=[], last_target="",
                    repeats=0, until=now)
        client.write_local("[sf2] starting: %s / %s" % (parts[0], parts[1]),
                           "#aa88cc")
        return

    if now < _sf2["until"]:
        return

    st = _sf2["state"]

    if st == "init":
        # Sent as three separate lines rather than one chain so a refusal is
        # visible in the log against the command that caused it.
        client.send_line("enter cabinet")
        client.send_line("select " + _sf2["difficulty"])
        client.send_line("choose " + _sf2["fighter"])
        _sf2.update(state="ask", until=now + SF2_STEP_DELAY * 3)
        return

    if st == "ask":
        # We are standing in the hub whenever we can run `progress`, so this
        # is the moment to learn which room "back at the start" means. Each
        # difficulty has its own hub (Hard is 49666).
        if getattr(client, "room_vnum", None):
            _sf2["hub"] = client.room_vnum
        client.send_line("progress")
        _sf2.update(state="read", lines=[],
                    until=now + SF2_PROGRESS_WINDOW)
        return

    if st == "read":
        entries = list(_sf2["lines"])
        if not entries:
            _sf2_stop(client, "no `progress` output - are we in the hub?",
                      "#ff5555")
            return
        nxt = _sf2_pending(entries)
        if nxt is None:
            _sf2_stop(client, "tournament complete - %d opponent(s) beaten"
                      % len(entries), "#66cc66")
            return
        # Spin guard: the same opponent twice running means the fight is not
        # registering (dead but not credited, wrong name, refused entry).
        if nxt == _sf2["last_target"]:
            _sf2["repeats"] += 1
            if _sf2["repeats"] >= 2:
                _sf2_stop(client, "stuck on %s - it never registers as beaten"
                          % nxt, "#ff5555")
                return
        else:
            _sf2["repeats"] = 0
        _sf2.update(target=nxt, last_target=nxt, state="enter",
                    until=now)
        return

    if st == "enter":
        ent, _chal = _sf2_names(_sf2["target"])
        client.write_local("[sf2] next: %s" % _sf2["target"], "#aa88cc")
        client.send_line("enter " + ent)
        _sf2.update(state="challenge", until=now + SF2_STEP_DELAY)
        return

    if st == "challenge":
        _ent, chal = _sf2_names(_sf2["target"])
        client.send_line("challenge " + chal)
        _sf2.update(state="kill", until=now + SF2_STEP_DELAY)
        return

    if st == "kill":
        _ent, chal = _sf2_names(_sf2["target"])
        client.send_line("kill " + chal)
        # `until` paces the poll; `deadline` bounds the whole fight. Setting
        # until to the timeout would have parked the script for the full
        # SF2_FIGHT_TIMEOUT before it ever looked at whether the fight ended.
        _sf2.update(state="fight", until=now + SF2_STEP_DELAY,
                    deadline=now + SF2_FIGHT_TIMEOUT)
        return

    if st == "fight":
        # The fight is over when the mud TELEPORTS US BACK to the hub - the
        # room where `progress` and `enter <fighter>` work. That is the
        # authoritative signal: `in_combat` going false would also fire if we
        # fled or the kill never registered, leaving us stranded in the
        # opponent's room sending `progress` at nothing.
        if now >= _sf2["deadline"]:
            _sf2_stop(client, "%s took longer than %ds - stopping"
                      % (_sf2["target"], int(SF2_FIGHT_TIMEOUT)), "#ff5555")
            return
        hub, here = _sf2["hub"], getattr(client, "room_vnum", None)
        if hub is not None and here is not None:
            back = (here == hub)
        else:                       # no room data - fall back to combat state
            back = not client.in_combat
        # Still hold for the guild's post-kill routine (corpse cascade, mw,
        # revalrie) so `progress` does not interleave with it.
        if not back or client._post_kill_holding():
            _sf2["until"] = now + SF2_STEP_DELAY
            return
        _sf2.update(state="ask", until=now + SF2_STEP_DELAY)
        return


def on_tick(client):
    _sf2_tick(client)
    name = str(client.vars.get("bot", "") or "").strip().lower()
    if name in ("", "0", "off"):
        if _bot["name"]:
            _bot.update(name="", state="idle", lines=[])
        return
    bot = BOTS.get(name)
    if bot is None:
        _bot_stop(client, f"no such bot '{name}'", "#ff5555")
        return
    now = time.time()

    if _bot["name"] != name:                 # fresh start
        _bot.update(name=name, i=0, kills=0, lines=[],
                    state="scan", until=now + SCAN_TIME)
        _bot["players"] = set()
        _refresh_who(client, now)
        client.write_local(
            f"[bot: '{name}' started - {len(bot['path'])} moves, "
            f"killing: {', '.join(bot.get('kill', [])) or 'nothing'}]",
            "#aa88cc")
        return                               # scan the room we are in

    pct = _hp_pct(client)
    stop_pct = bot.get("stop_pct", 0)
    if stop_pct and pct is not None and pct < stop_pct:
        if bot.get("stop_cmd"):
            client.process_input(bot["stop_cmd"])
        _bot_stop(client, f"hp {pct:.0f}% < {stop_pct}% - stopping",
                  "#ff5555")
        return

    if now - _bot["who_t"] > WHO_REFRESH:
        _refresh_who(client, now)

    if client.in_combat:
        _bot["state"] = "fight"
        return

    state = _bot["state"]
    if state == "fight":                     # combat just ended
        bot_after = bot.get("after_kill", "")
        if bot_after:
            client.process_input(bot_after)
        if bot.get("resume_on") and not _bot["regen_done"]:
            _bot["state"] = "regen"          # hold for the refresh text
            _bot["until"] = now + bot.get("resume_timeout", 90)
            return
        _bot["lines"] = []
        _bot["state"] = "scan"
        _bot["until"] = now + SCAN_TIME
        client.send_line("look")             # re-scan for more mobs
        return
    if state == "regen":
        if _bot["regen_done"] or now >= _bot["until"]:
            if not _bot["regen_done"]:
                client.write_local("[bot: refresh text never came - "
                                   "resuming anyway]", "#888899")
            _bot["regen_done"] = False
            _bot["lines"] = []
            _bot["state"] = "scan"
            _bot["until"] = now + SCAN_TIME
            client.send_line("look")
        return
    if now < _bot["until"]:
        return
    if state == "scan":
        _bot_evaluate_room(client, bot, now)
    elif state == "kill_wait":               # combat never started
        client.write_local("[bot: no such mob here - moving on]",
                           "#888899")
        _bot_send_move(client, bot, now)


def on_connect(client):
    _portal_closed(client)
    client.vars["bot"] = "off"
