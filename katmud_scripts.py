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


def on_vitals(client, v):
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


def on_tick(client):
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
