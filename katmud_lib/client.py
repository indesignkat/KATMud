"""katmud_lib.client - the KatMUD client window.

One process per character (spawned detached by the picker, or directly
via `katmud.pyw <profile-id>`). Ported from pymud v6 and rebuilt on
the v7 config cascade:

  * all triggers/aliases/keys/gags/MIP handlers come from the merged
    cascade (global -> mud -> guild -> character)
  * passwords come from the OS credential store, prompting and storing
    on first connect, re-prompting on rejection (spec 3)
  * keybindings are cascade data keyed by keysym (spec 4.1)
  * MIP handler registry is built once at startup from the cascade
    (spec 5); guild change = relaunch
  * map loads with cascade graph patches applied (spec 6.2); mapping
    mode creates rooms from MIP data + rating capture (6.3/6.5) with
    [House] rooms scoped to the character (6.2.1)
"""

import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
import urllib.request
from collections import deque, namedtuple
from tkinter import font as tkfont
from tkinter import simpledialog

from . import (blade, changeling, config, credentials, dialogs, mapdata,
               mapparse, mapsql, mobdb, necro, paths, profiles, viking)
from .protocol import (MudConnection, parse_composite,
                       strip_mip_colors, tag_to_style)
from .widgets import MapPane, VitalsBar

try:
    import winsound
except ImportError:
    winsound = None


def play_sound(path):
    """'beep', 'off'/'' (silent), or a .wav file path."""
    if not path or path == "off":
        return
    if winsound:
        try:
            if path == "beep":
                winsound.MessageBeep()
            else:
                winsound.PlaySound(path, winsound.SND_FILENAME |
                                   winsound.SND_ASYNC |
                                   winsound.SND_NODEFAULT)
        except RuntimeError:
            pass


DAMAGE_RE = re.compile(
    r"^You hit (.+?) (\d+) times? for ([\d,]+) damage\.")
MISSION_FULFILL_RE = re.compile(r"^\s*vmission\s+fulfill\s+(\d+)",
                                re.IGNORECASE)
NEWBIE_ACCEPT_RE = re.compile(
    r"^\s*vmission\s+newbie\s+accept\s+(\d+)", re.IGNORECASE)
DEFAULT_KILL_PATTERNS = [r"dealt the killing blow to (.+?)\.\s*$"]
_DIR_OPPOSITE = {
    "n": "s", "s": "n", "e": "w", "w": "e", "u": "d", "d": "u",
    "ne": "sw", "sw": "ne", "nw": "se", "se": "nw",
}
# 3k shows a multiplicity tag like "A Spiral Gun {2}." when more than one of
# the same mob/item is in the room - strip it before matching a bot whitelist
# entry, or an exact-match Run bot never recognizes the mob at all.
RUN_COUNT_TAG_RE = re.compile(r"\s*\{\d+\}\s*$")
# A room-entry mob line already mid-attack, e.g. 'Flesh Golem attacking
# you!.' or 'Steel Golem [scratched] attacking you!.' - the mud's
# auto-combat already has these on you regardless of whether they're on
# the bot's kill list, which _run_scan_mob's exact-name match never sees
# (the suffix breaks the match). Caught separately so Run holds movement
# instead of reading the room as clear and walking off mid-fight.
RUN_ATTACKING_RE = re.compile(
    r"^(.+?)(?:\s+\[[^\]]*\])?\s+attacking you!\.?\s*$", re.IGNORECASE)
# Max times one trigger may fire in a row (same pattern, no other trigger or
# manual command in between) before we suppress it as a runaway loop.
TRIGGER_RECURSION_LIMIT = 10

DEFAULT_VITALS = [
    {"label": "HP", "cur": "hp", "max": "hpmax", "color": "#33aa33",
     "width": 190, "warn": [[0.25, "#cc3333"], [0.5, "#cc9933"]]},
    {"label": "SP", "cur": "sp", "max": "spmax", "color": "#3366cc",
     "width": 190},
    {"label": "GP1", "cur": "gp1", "max": "gp1max", "color": "#aa8833",
     "width": 190},
    {"label": "GP2", "cur": "gp2", "max": "gp2max", "color": "#8833aa",
     "width": 190},
    {"label": "Enemy", "enemy": True, "color": "#aa3333", "width": 260},
]

# Windows VK_NUMPAD keycodes (NumLock ON) -> canonical KP keysym.
WIN_NUMPAD_KEYCODES = {
    96: "KP_0", 97: "KP_1", 98: "KP_2", 99: "KP_3", 100: "KP_4",
    101: "KP_5", 102: "KP_6", 103: "KP_7", 104: "KP_8", 105: "KP_9",
    107: "KP_Add", 109: "KP_Subtract", 106: "KP_Multiply",
    111: "KP_Divide", 110: "KP_Decimal",
}
KP_KEYSYMS = {
    "KP_0", "KP_1", "KP_2", "KP_3", "KP_4", "KP_5", "KP_6", "KP_7",
    "KP_8", "KP_9", "KP_Add", "KP_Subtract", "KP_Multiply",
    "KP_Divide", "KP_Decimal", "KP_Insert", "KP_End", "KP_Down",
    "KP_Next", "KP_Left", "KP_Begin", "KP_Right", "KP_Home", "KP_Up",
    "KP_Prior", "KP_Delete",
}

# Rating-response swallow patterns (spec 6.5), confirmed against live
# 3s output 2026-06-12. The response burst is:
#   AREA NAME: <name> [<coder>]          (absent in overland rooms!)
#   AREA RATING -> <volatile text>        (always; discard - stale)
#   Monster class range since inception: ...   (optional)
#   Monster class range for this boot  : ...   (optional)
# Only these patterns are eaten while a client-initiated rating is
# pending; anything else flows through (conservative). Extra patterns
# via settings.rating_swallow_patterns.
DEFAULT_RATING_SWALLOW = [
    r"^Monster class range",
]
# Leading [\s>]* tolerates a prompt stuck to the front of the line: 3s
# doesn't always GA-terminate the prompt before the rating reply, so the
# line can arrive as "> AREA NAME: ..." - the old ^AREA anchor missed those,
# which is why some rooms got an area and others stayed NULL.
AREA_LINE_RE = re.compile(r"^[\s>]*AREA NAME:\s*(.+?)\s*$")
RATING_LINE_RE = re.compile(r"^[\s>]*AREA RATING\s*->")
RATING_TIMEOUT = 5.0

REVERSE_DIR = {"n": "s", "s": "n", "e": "w", "w": "e",
               "ne": "sw", "sw": "ne", "nw": "se", "se": "nw",
               "u": "d", "d": "u", "up": "down", "down": "up"}

class MudClient:
    def __init__(self, root, entry, profiles_data):
        self.root = root
        self.profiles_data = profiles_data
        self.entry_data = entry
        self.profile_id = entry["id"]
        self.display_name = entry.get("display_name") or entry["id"]
        self.character = entry["character"]
        self.mud = entry["mud"]
        self.guild = entry.get("guild", "none") or "none"

        self.cascade = config.Cascade(self.mud, self.guild,
                                      self.character)
        conn_cfg = self.cascade.get("connection", {}) or {}
        self.host = conn_cfg.get("host", "3scapes.org")
        self.port = entry.get("port_override") or conn_cfg.get("port",
                                                               3200)
        self.mip_enabled = conn_cfg.get("mip", True)

        self.conn = None
        self.queue = queue.Queue()
        self.echo_on = True
        self.log_file = None
        self.history, self.history_pos = [], None
        # Prefix anchored when Up-arrow history navigation starts: Up/Down
        # then cycle only history entries that start with it (empty = all).
        self.history_prefix = None
        self.hooks = None
        self._known_tags = set()
        self.mip_pin = random.randint(10000, 99999)
        self.mip_sent = False
        self.password_sent = False
        self._pending_store_pw = None
        self._logged_in = False

        # top-bar state
        self.uptime = "?"
        self.mudlag = "?"
        self.reboot_eta = "?"
        self.room = "?"
        self.exits = []
        self.rounds = 0
        self.in_combat = False
        self.last_sent = time.time()
        self.last_manual = time.time()
        self.deadman_tripped = False
        # Cross-character reminders: shared reminders.json polled each tick.
        # Cache the parsed list and only re-read when the file's mtime moves
        # (another client/character may add or fire one at any time).
        self._reminders_cache = []
        self._reminders_mtime = None
        # Phone/web dashboard (spec: docs/superpowers/specs/
        # 2026-06-21-web-dashboard-design.md): ring buffers snapshotted to
        # webstate/<profile_id>.json every tick. Bounded so the file stays
        # small - this is scrollback for a glance, not a session log.
        self._web_output = deque(maxlen=200)
        self._web_chats = deque(maxlen=50)
        self._web_tells = deque(maxlen=50)
        self.connected_at = None
        self.vitals = {}
        self.frozen = False
        self.status_text = "disconnected"
        self.stat1 = ""
        self.stat2 = ""
        self.room_vnum = None
        self.mipraw = False
        self.mipraw_buf = []
        self.markers_debug = False      # #markers: report italic/underline
                                        # lines (mob/player room markers)
        self.viking_state = {}          # merged BBE feed (guild MIP)
        self.viking_win = None
        self.viking_spells = ""         # active-spell status line
        self.viking_skills = None       # last parsed `vskills` (skill costs)
        self._vskills_capture = False   # arming flag while reading `vskills`
        self._vskills_lines = []        # accumulated `vskills` output lines
        self._vtradestock_capture = False  # arming flag for `vtrade stock`
        self._vtradestock_lines = []    # accumulated `vtrade stock` lines
        self._vtrade_stock = {}         # last parsed `vtrade stock` totals
        self._missionlist_capture = False  # arming flag for `vmission list`
        self._missionlist_lines = []    # accumulated `vmission list` lines
        self._missionlist_saw_board = False  # True once 'Global Mission Board' seen
        self._mission_picks = []        # current Missionlist recommendation
        self._vnlist_capture = False    # arming flag for `vmission newbie`
        self._vnlist_lines = []         # accumulated `vmission newbie` lines
        self._vnlist_metric = "daler"   # VNlist's optimization target
        self._newbie_picks = []         # current VNlist recommendation
        self._viking_focus_bind = None  # root <FocusIn> -> raise status win
        self.changeling_status = ""     # changeling FFF I-field shown in the
                                        # status bar (Flux/Density/FF/form)
        self.blade_status = ""          # bladesinger Mode/Eff/Blur/G2N line
                                        # shown in the status bar
        self.necro_status = ""          # necro Status[w/p/v/r] Cr[..] line
                                        # shown in the status bar
        self.changeling_forms = {}      # {name_lower: {name,fam,next}} from
                                        # the `forms` table (attack-form pick)
        self.changeling_form_points = 0  # `Available Points` from `forms`
        # one-shot "run once a fresh guild-map position arrives" hook, used
        # by VN to wait out a city enter/leave before pathing the next leg.
        self._viking_pos_cb = None
        self._viking_pos_job = None
        self._viking_pos_armed = 0.0
        # Necromancer "Track" tracker (see necro.py). tracked = {name: low}
        # (low threshold or None), persisted per-character; necro_counts =
        # latest known value for any power/reagent/corpse name (runtime).
        self.tracked = {}
        self.necro_counts = {}
        self._necro_powers_capture = None   # accumulator during a `powers` read
        self._necro_auto_last = {}          # auto-command cooldown timestamps
        # `Reagents` restock: while _reagents_target is set we capture the
        # fresh `gs` reagent counts, then buy each up to the target.
        self._reagents_target = None
        self._reagents_have = {}
        self.vars = {}
        self.kills = []
        self.merc_state = {}    # latest BBC mercenary feed (see mip_mercenary)
        self.last_unknown_mip = ""
        self._fff_combat = False
        self._hold_job = None
        self.dmg = {"portals": dict(rounds=0, hits=0, damage=0),
                    "noportals": dict(rounds=0, hits=0, damage=0)}
        self.vitals_max = {}
        self._maxes_dirty = False
        self.last_deltas = ""
        self.blur_portal = ""
        # Bladesinger skill-GXP tracker (see blade.py). blade_skill_costs =
        # {name: cost-or-None} from the last `skills` readout; blade_available
        # = live spendable GXP derived from the prompt G2N line; blade_glvl =
        # current guild level. Shared `tracked` dict names a skill to watch.
        self.blade_skill_costs = {}
        self.blade_available = None
        self.blade_glvl = None
        # Blur-reset duration estimator (Part C): anchor a (pct, time) sample
        # at the start of a reset cycle and extrapolate the full 0->100 time.
        self._blur_anchor = None        # (pct, t) | None
        self.blur_reset_secs = None     # learned full reset duration (sec)
        # Auto-blur (Part D): fire the FIRST blur late in a reset cycle so all
        # `max` charges get spent before the reset refills them - an unused
        # charge at refill is wasted GXP + defense. The fade trigger ("...
        # disintegrates into nothingness" -> blur) chains the rest, so only the
        # kickoff needs deciding. secs_per_charge = self-measured wall time to
        # burn one charge while fighting (interval between successive B-count
        # drops); kickoff threshold% = 100*(1 - max*secs_per_charge/reset_secs)
        # - margin, i.e. start just early enough for all charges to cycle.
        self.blur_auto = True              # master switch (Toggle blur)
        self.blur_secs_per_charge = None   # learned single-blur lifetime (sec)
        self._blur_last_drop = None        # (n, t) of the last B-count decrement
        self._blur_auto_last = 0.0         # cooldown stamp for the kickoff send
        self.blur_auto_pct_eff = None      # last computed threshold (pane hint)
        self.status_notes = []          # one-time status-pane warnings
        self._warned_fields = set()
        self.guild_login_sent = False

        self.load_layers()

        # --- map / speedruns / landmarks ---
        self.tmap = None
        self.locator = None
        self.speedruns = {}
        self.landmarks = {}
        self.walk = []
        self._walk_job = None
        self._walk_done_cb = None       # fires once a walk reaches its goal
        self._last_move = None
        # --- area roaming bot (Bot / Hunt commands) ---
        self._bot_on = False
        self._bot_mode = "aggro"        # "aggro" (wait for aggro) | "hunt"
        self._bot_mapless = False       # `Bot mapless`/`Hunt mapless`: no
                                        # sqlite map needed, roam by direction
                                        # only (see _bot_pick_move_mapless)
        self._bot_last_dir = None       # mapless-only: last direction moved
                                        # (avoid immediately reversing it)
        self._bot_area_id = None
        self._bot_area_name = ""
        self._bot_start_vnum = None     # room Bot was started in (return point)
        self._bot_prev_vnum = None      # room we came from (avoid backtrack)
        self._bot_fence_vnum = None     # `Hunt <dir>`: origin room; bot may not
        self._bot_fence_dir = None      #   exit it in this direction
        self._bot_in_combat = False     # was the last tick a combat tick?
        self._bot_combat_end = 0.0      # when the last fight ended
        self._bot_homing = False        # running the post-deadman return walk
        self._bot_home_reason = ""      # _bot_stop() message once home (deadman
                                        # vs. area-cleared - whichever armed it)
        self._bot_recheck = False       # re-look this room (start / post-combat)
        self._bot_no_combat_rooms = 0   # Hunt-only: consecutive rooms entered
                                        # with no fight; resets on any combat
        self._hunt_attacked = False     # swung at this room's mob, waiting for
                                        # the fight to register (autocombat then
                                        # finishes it - don't roam meanwhile)
        self._hunt_attacked_at = 0.0    # when that swing was sent
        self._hunt_whiffs = 0           # consecutive swings that never engaged
                                        # (bad keyword); cap stops a dead loop
        self._hunt_lines = []           # recent room text (keyword-parse fallback)
        self._bot_job = None
        # --- marker-driven room assessment: the MUD asets look_monster
        # (italic, SGR3) / look_player (underline, SGR4) so the room's
        # auto-look flags mobs / players instantly; the bot reads those
        # instead of waiting + parsing. Flags are accumulated as the room
        # text streams in (poll_queue) and read on the prompt that ends it. ---
        self._bot_waiting_room = False  # sent a move/look, awaiting the display
        self._bot_room_ready = False    # prompt/packet seen -> display complete
        self._bot_ready_at = 0.0        # when the room became ready
        self._bot_room_t0 = 0.0         # when we entered (for debug timing)
        self._bot_first_line_at = 0.0   # first room line after a move (wire RTT)
        self._bot_debug = False         # `Bot debug`: per-room timing report
        self._bot_room_mob = False      # an italic (mob) line seen this room
        self._bot_room_player = False   # an underline (non-party player) seen
        self._bot_mob_lines = []        # the italic lines (for kill keyword)
        # --- Hunt-only: the 3s minimap cross ("-O-"/"-1-"/"-*-" cells one
        # and two rooms away in each direction, magenta=mob/green=player,
        # "#"=charted dead end) read during the same room-wait window, to
        # bias _bot_pick_move toward mobs and away from known-empty dead
        # ends. ---
        self._bot_minimap = {}          # direction -> _MMCell
        self._mm_pending = []           # [(col, char, spans), ...] lone "-X-"
                                        # rows seen before the @ row, oldest
                                        # first, capped at 2 (N2 then N1)
        self._mm_south_col = None       # set once the @ row is seen; the
                                        # next 2 lone "-X-" rows at this
                                        # column are south (S1 then S2)
        self._mm_south_count = 0        # how many south rows captured so far
        # party whitelist: auto roster from `pwho` + manual bot_party_whitelist
        self._bot_party = set()         # lowercase names from the last pwho
        self._bot_pwho_at = 0.0
        self._pwho_capturing = False
        self._pwho_buf = []
        # --- Chaossea: Sea of Chaos item hunt (mapless temp-map + examine/
        # target/loot/retreat loop). See docs/superpowers/specs/2026-06-20-
        # mapless-wander-and-chaossea-design.md. ---
        self._chaossea_on = False
        self._chaossea_fight_mode = False  # True = `Chaossea fight`: kill
                                           # every mutant, ignore loot/items
        self._chaossea_job = None
        self._chaossea_map = {}         # fake_vnum -> {direction: fake_vnum}
        self._chaossea_room_exits = {}  # fake_vnum -> live exits seen there
                                        # (for BFS frontier-seeking)
        self._chaossea_cur = "f00001"
        self._chaossea_next_id = 1
        self._chaossea_last_dir = None
        self._chaossea_has_charm = False
        self._chaossea_has_cube = False
        self._chaossea_room_mobs = 0    # mutant count seen this room
        self._chaossea_mob_idx = 0      # which ordinal (1-based) we're on
        self._chaossea_examine_buf = []
        self._chaossea_examine_pending = False
        self._chaossea_waiting_room = False
        self._chaossea_room_ready = False
        self._chaossea_room_mob = False
        self._chaossea_room_player = False
        self._chaossea_mob_lines = []
        self._chaossea_pre_move_room = None  # set (to self.room) just before
                                             # a move attempt; None means no
                                             # move is pending a blocked-check
        self._chaossea_got_ddd = False  # a DDD landed since the last move was
                                        # sent - the move-succeeded signal (see
                                        # _chaossea_handle_blocked: every room
                                        # in Sea of Chaos shares one name+vnum,
                                        # so comparing self.room can't tell a
                                        # real move from a rejected one)
        self._chaossea_pre_move_mobs = 0    # examined mut count before a move,
                                            # snapshotted before _chaossea_enter_room
                                            # zeroes the live counter
        self._chaossea_room_done = False   # every mutant in this room checked
        self._chaossea_pending_loot = None  # items the just-killed mutant had
                                            # (None = not currently fighting
                                            # one - distinct from [], "fought
                                            # one that carried nothing we need")
        self._chaossea_was_fighting = False
        self._chaossea_post_kill_wait = False  # a kill landed; holding for
                                               # the guild's post-kill resume
                                               # gate (e.g. vrest) before loot
        self._chaossea_engage_at = 0.0      # time.time() when the last kill was sent
        self._chaossea_engage_whiffs = 0    # consecutive kills that never engaged
        self._chaossea_exits_retry = 0      # consecutive bare-DDD 'look' retries
                                            # for the current room (see
                                            # _chaossea_exits_unknown_retry)
        # --- path bot (Run): imported tintin steppers, fixed route + a
        # per-area target whitelist; map-independent so it works on 3k. ---
        self._run_on = False
        self._run_name = ""
        self._run_path = []             # ordered step strings
        self._run_idx = 0
        self._run_loop = False
        self._run_mobs = []             # [(match_lower, target)]
        self._run_setup = []
        self._run_lines = []            # room text since the last step
        self._run_room_enter_t = 0.0    # time.time() this room's lines started
        self._run_acted = False         # already mob-checked this room?
        self._run_killing = False       # in/awaiting a fight right now
        self._run_kill_start = 0.0      # when the current kill/fight started
        self._run_engaged = False       # in_combat seen true this fight
        self._run_combat_end = 0.0
        self._run_kills = 0             # stats only (kill_pattern count)
        self._run_started = 0.0
        self._run_job = None
        self._run_after_kill = []       # commands sent when a fight ends (loot)
        self._run_skip = []             # room substrings that make Run skip it
        self._run_room_player = False   # non-party player marker seen this room
        self._run_precheck = None       # pending {"cmd","fail_contains",...}
        self._run_precheck_job = None   # scheduled settle/timeout for it
        # --- guild macro runner: gated multi-step sequences (e.g. the
        # bladesinger "Autoregen <item>" rune chain) defined per-guild in
        # the cascade's "macros" section. Each step sends its command, then
        # (if it has a `wait` substring) holds until that text is seen or
        # the per-step timeout fires; a step with wait=None fires and the
        # runner advances immediately, no hold. ---
        self._macro_on = False
        self._macro_name = ""
        self._macro_steps = []          # [(cmd, wait_lower_or_None), ...]
        self._macro_idx = 0
        self._macro_job = None
        # --- post-kill resume gate: a GUILD setting (post_kill_resume_text),
        # not per-bot. When set (e.g. bladesinger revalrie), every bot engine
        # holds after a kill until that text arrives or the timeout elapses. ---
        self._resume_pending = False
        self._resume_deadline = 0.0
        self._resume_text = ""
        self._resume_vitals = []        # vitals-gated alternative (e.g. viking
        self._resume_vitals_margin = 20  # vrest: no end text, just wait for
                                        # hp/seid/vig/rad to top back up)
        # --- Explore: auto-chart an area's open stubs. Probes each cardinal/
        # ordinal stub through the charting gate (one gated move at a time);
        # short hops to the next stub (<=2 rooms) stay gated so the probe's
        # from-room is always packet-confirmed, and only a jump of >2 rooms drops
        # charting for a fast, arrival-verified walk across the mapped region.
        # Non-directional stubs (up/down/enter/out/special) are
        # flagged for manual review. Adjudicable mischarts (two exits from one
        # room to one target; two rooms reaching one target the same direction)
        # are auto-re-charted off room-ids + exit topology alone. Unrated rooms
        # are walked-to and rated: a room that rates into this area folds in
        # (its stubs become reachable), an overland border rates area-less
        # ('Caution is Advised') and is left. `Mapcheck` reports it all read-
        # only. ---
        self._explore_on = False
        self._explore_area_id = None
        self._explore_area_name = ""
        self._explore_start_n = 0       # stub count at start (for the report)
        self._explore_tried = set()     # (from_vnum, direction) done/failed
        self._explore_recharted = set()  # (from,dir) re-stubbed to fix a dupe
                                        # this run (bounds the re-chart loop)
        self._explore_flagged = set()   # (from,dir) reported for manual review
        self._explore_rated_tried = set()  # unrated rooms we walked to + rated
        self._explore_rating = None     # vnum we walked to and are rating now
        self._explore_rate_back = None  # reverse of the entry edge, to retreat
                                        # from a room that rated foreign
        self._explore_plan = []         # remaining commands for the current run
        self._explore_target = None     # the stub being probed right now
        self._explore_travel = False    # walking to a distant stub room with
                                        # charting OFF (fast); re-armed to probe
        self._explore_travel_dest = None  # vnum a charting-off leg is heading
                                        # to - verified on arrival before probe
        self._explore_travel_wait = 0   # ticks waited for the arrival to settle
        self._explore_entered_chart = False
        self._explore_job = None
        # chartwalk extras (Chart <entry>): bootstrap into a fresh area via an
        # entry command, run Explore over it, then return to where you started.
        self._explore_phase = "explore"  # "bootstrap" | "explore" | "return"
        self._explore_return_to = None   # vnum to walk back to when done (or None)
        self._explore_simple = False     # only take compass exits
        self._explore_entry = ""         # the entry command used (for the report)
        self._explore_return_cmd = None  # optional explicit return command
        # --- Agent: the autonomous assistant (see agent_spec.md). One self-
        # rescheduling tick loop that picks the highest-priority goal each
        # tick (chart -> hunt -> survive -> home-on-deadman) driven by a per-
        # guild profile. SCAFFOLD STAGE: the tick names its chosen goal but
        # does not act yet; behaviour lands guild-by-guild (Changeling first).
        self._agent_on = False
        self._agent_mode = "full"        # "full" | "hunt" | "explore"
        self._agent_phase = "chart"      # phased plan: "chart" then "sweep"
        self._agent_area_id = None
        self._agent_area_name = ""
        self._agent_start_vnum = None    # room Agent was started in (return point)
        self._agent_goal = ""            # the goal chosen on the last tick
        self._agent_debug = False        # `Agent debug`: per-tick goal report
        self._agent_job = None
        self._agent_homing = False       # running the post-deadman walk home
        self._agent_explore_started = False  # chart phase: kicked off Explore
        self._agent_explore_retries = 0  # chart phase: Explore start attempts
        self._agent_hunt_started = False     # sweep phase: kicked off Hunt
        self._agent_recharting = False   # re-charting after Hunt boxed itself in
        self._agent_prechart_rooms = 0   # area room count before a re-chart
        self._agent_gate_until = 0.0     # waiting for score before the sweep
        self._agent_heal_at = 0.0        # last heal action (cooldown)
        self._agent_flee_at = 0.0        # last flee action (cooldown)
        self._agent_form_at = 0.0        # last form-ensure (cooldown)
        self.area_class_top = None       # top monster class of the current area
                                         # (from the rating "class range" line)
        self.changeling_best_kill = None  # best-kill class from plain `score`
        # --- trigger runaway guard: a trigger whose command produces output
        # that matches the same trigger again can loop forever (feedback over
        # the wire). Count consecutive fires of one pattern; once it passes
        # TRIGGER_RECURSION_LIMIT we suppress the command until a *different*
        # trigger fires or the user sends a manual command (which resets it). ---
        self._trig_run_pattern = None   # pattern of the current consecutive run
        self._trig_run_count = 0        # how many times it has fired in a row
        self._trig_run_warned = False   # warned once for this runaway episode
        # Global flood breaker: the consecutive guard above misses an
        # ALTERNATING loop (trigger A's output fires B, B's fires A, ...), which
        # keeps resetting the consecutive run. This counts ALL trigger fires in
        # a sliding window regardless of pattern; past the limit it trips and
        # PAUSES all trigger firing until reload/relaunch. Plus an optional
        # per-trigger "cooldown" (seconds) so one trigger can't refire on its
        # own echo. Reset in load_layers (so #reload re-arms).
        self._trig_fires = deque()      # timestamps of recent trigger fires
        self._trig_tripped = False      # flood breaker has tripped
        self._trig_cooldowns = {}       # pattern -> last fire time
        # --- mob database (vscan1 capture, see mobdb.py) ---
        self._vscan_buf = []            # accumulating box lines
        # mapping / record state (spec 6.3)
        self.mapping = False
        self.mapping_auto = False
        self._session_new_rooms = set()
        self._record_scope = None
        self._rating_pending_rid = None
        self._rating_sent_at = 0.0
        self._pending_persist = None
        # --- sqlite map backend (3s redesign); "tin" keeps the legacy
        # TinMap path untouched. A process is one or the other, never
        # both, so the two paths branch rather than share state. ---
        self.map_backend = self.setting("map_backend", "tin")
        self.mapdb = None
        # `Toggle mapdetail`: include the current room's exits + notes in
        # the lower-right info pane. Persisted per-character (Toggle saves),
        # so it survives a reboot (off by default; see _mapdetail_lines).
        self.show_mapdetail = bool(self.setting("show_mapdetail", False))
        # `Toggle flat`: Hunt only takes cardinal/ordinal exits (skips u/d),
        # for areas that get harder as you descend. Persisted; off by default.
        self.hunt_flat = bool(self.setting("hunt_flat", False))
        # Hunt-only "area cleared" safety net: after this many consecutive
        # rooms with no combat, walk back to the start room (re-pathfinding
        # via _bot_home_step, same as the deadman return) and stop. 0 = off.
        self.hunt_clear_limit = int(self.setting("hunt_clear_limit", 30) or 0)
        self.wander_clear_limit = int(
            self.setting("wander_clear_limit", 30) or 0)
        # --- two map modes (see the `Chart` command) ---
        # FOLLOWING (default): the current room follows the vnum in each
        # DDD packet. The client never writes the map, so a fast walk can
        # lag ~1s but can neither lose you (the next DDD re-pins) nor chart
        # a wrong edge (it charts nothing at all). CHARTING (`Chart`
        # toggle): the only mode that writes. Outgoing commands are
        # buffered and released ONE at a time, each gated on the
        # slow-but-authoritative BAD packet, so the player can never
        # outrun the charting.
        self._sql_cur_vnum = None       # current room (DDD-confirmed)
        # 3s sometimes truncates the MIP tilde id (drops leading digits);
        # only the BAD bracket carries the full id. Two recovery maps, both
        # feeding _sql_resolve_low5 so bracket-less DDD packets resolve to the
        # full id: low-5 -> high part (6-digit rooms, seeded from the db) and
        # an OBSERVED truncated-tilde -> full map (any width, learned only
        # from real bracket mismatches so a genuine low room is never lifted).
        self._sql_vnum_hi = {}
        self._sql_trunc = {}
        self._sql_charting = False      # chart mode on? (Chart / #map on)
        self._sql_pending = None        # new room held until rating
        self._sql_in_house = False      # inside a [House]; not charted
        self._sql_bad = None            # last BAD payload, awaiting DDD
        self._sql_ddd = None            # last DDD payload, awaiting BAD
        self._sql_rating_pending = False  # rating sent, awaiting name
        self._sql_rating_active = False   # name seen; swallow the burst
        self._sql_rate_overland = False   # last rating was an overland room
                                          # ('AREA RATING -> ... [Mud]')
        # charting lockstep gate
        self._sql_chart_buf = deque()   # commands queued behind the gate
        self._sql_gate_open = True      # may the next queued cmd be sent?
        self._sql_gate_job = None       # no-BAD timeout handle (after id)
        self._sql_chart_from = None     # room the in-flight cmd was sent from
        self._sql_chart_move = None     # the in-flight cmd (candidate edge)
        self._sql_combat_hold_job = None  # retry handle while combat holds gate
        self._sql_rating_job = None       # hard fallback so a rating reply
        #                                   that never parses can't stall the
        #                                   gate (see sql_send_rating)
        self._sql_chart_debug = False     # #chartdebug: trace gate events
        self._sql_late_move = None        # (from, via) of a move whose no-BAD
        #                                   window elapsed before its packet;
        #                                   a late arrival still charts it
        self._sql_chart_combat_paused = False  # charting auto-paused for a
        #                                   fight; resume when it ends
        self._sql_chart_resume_job = None  # debounced post-combat resume timer

        self.build_ui()
        self.load_scripts(announce=False)
        self.start_map_loader()
        self.root.after(30, self.poll_queue)
        self.root.after(1000, self.tick)
        self.connect()

    # ------------------------------------------------ config layering
    @staticmethod
    def compile_triggers(items):
        out = []
        for t in items:
            try:
                cd = float(t.get("cooldown", 0) or 0)
                out.append((t["pattern"], re.compile(t["pattern"]),
                            t.get("command", ""), t.get("sound", ""), cd))
            except (re.error, KeyError, ValueError, TypeError):
                pass
        return out

    @staticmethod
    def compile_gags(patterns):
        out = []
        for p in patterns:
            try:
                out.append((p, re.compile(p)))
            except re.error:
                pass
        return out

    def setting(self, key, default=None):
        return (self.cascade.get("settings", {}) or {}).get(key,
                                                            default)

    def set_setting(self, key, value, scope="character"):
        err = self.cascade.save_entry(scope, "settings", key, value)
        if err:
            self.write_local(f"settings save failed: {err}", "#cc6666")

    def load_layers(self):
        c = self.cascade
        self.aliases = dict(c.get("aliases", {}) or {})
        trig_items = c.get("triggers", []) or []
        self.triggers = self.compile_triggers(trig_items)
        # re-arm the flood breaker on (re)load - "fix the trigger and #reload".
        # Direct assignment (not .clear()) so it's safe on the first
        # load_layers, which runs before __init__ sets these.
        self._trig_fires = deque()
        self._trig_tripped = False
        self._trig_cooldowns = {}
        self.trigger_modes = {
            t["pattern"]: t["mode"] for t in trig_items
            if t.get("mode") in ("party", "noparty") and "pattern" in t}
        # Per-trigger loop override: "recursion_limit" raises (or removes, with
        # 0 = unlimited) the consecutive-fire cap for THIS pattern AND exempts
        # it from the global flood breaker. For MUD-forced loops that must fire
        # hundreds/thousands of times (e.g. "the rocks crumble beneath your
        # feet" -> out, which self-terminates when an `out` finally succeeds).
        self.trigger_limits = {}
        for t in trig_items:
            if "recursion_limit" in t and "pattern" in t:
                try:
                    self.trigger_limits[t["pattern"]] = \
                        max(0, int(t["recursion_limit"]))
                except (ValueError, TypeError):
                    pass
        # A trigger may carry "toggle": "<name>" to be gated by a runtime
        # Toggle switch (e.g. the necro kill-harvest trigger -> Toggle
        # harvest). pattern -> toggle name; on/off state in setting
        # "trigger_toggles" (default on), shown in the Toggles list.
        self.trigger_toggle_of = {
            t["pattern"]: t["toggle"] for t in trig_items
            if t.get("toggle") and "pattern" in t}
        self.trigger_toggles = dict(self.setting("trigger_toggles", {})
                                    or {})
        self.keys = dict(c.get("keys", {}) or {})
        self.gags = self.compile_gags(c.get("gags", []) or [])
        self.chat_gags = self.compile_gags(c.get("chat_gags", []) or [])
        kp = c.get("kill_patterns", []) or DEFAULT_KILL_PATTERNS
        self.kill_res = self.compile_gags(kp)
        # On-kill loot routine (first-class, replaces the per-guild
        # "dealt the killing blow to" trigger): {solo, party, toggle}. Fired
        # by _fire_corpse from the kill detection in track_combat_line.
        self.corpse_cfg = dict(c.get("corpse", {}) or {})
        vit = c.get("vitals")
        self.vitals_cfg = vit if isinstance(vit, list) and vit \
            else DEFAULT_VITALS
        self.login_commands = list(c.get("login_commands", []) or [])
        # Mud-wide login-time setup (additive across layers, not clobbered
        # by a guild's login_commands): 3s needs the room-marker asets and
        # DISPLAY_ROOMID set for every character regardless of guild.
        self.setup_commands = list(c.get("setup_commands", []) or [])
        # Track watch-list is per-mud-AND-guild: the character file is shared
        # across muds (3s-Din + 3k-Din), and blade skill names vs necro
        # power/reagent names must not intermingle across a gswap. No fallback
        # to a shared "tracked" - a fresh mud+guild starts EMPTY rather than
        # inheriting stale entries (e.g. blade's iron runes leaking onto necro).
        self.tracked = dict(self.setting(self._tracked_key(), {}) or {})
        self.necro_guard = bool(self.setting("necro_guard", True))
        self.blur_reset_secs = self.setting("blur_reset_secs")
        self.blur_auto = bool(self.setting("blur_auto", True))
        self.blur_secs_per_charge = self.setting("blur_secs_per_charge")
        # MIP registry: built ONCE at startup (spec 5). tag -> decl.
        self.mip_registry = dict(c.get("mip", {}) or {})
        char_layer = c.layers.get("character") or {}
        self.vitals_max = dict(char_layer.get("seen_max", {}))
        self.deadman_minutes = self.setting("deadman_minutes", 15)
        self.rating_swallow = self.compile_gags(
            self.setting("rating_swallow_patterns",
                         DEFAULT_RATING_SWALLOW))

    # ------------------------------------------------ UI construction
    def build_ui(self):
        self.root.title(f"KatMUD \u2014 {self.display_name} "
                        f"({self.guild}) {self.host}:{self.port}")
        s0 = self.profiles_data.setdefault("settings", {})
        # Restore the last size/position (the +X+Y in a Tk geometry string is
        # virtual-desktop coords, so this also restores which monitor). Saved
        # on close; restored as-is so a second-monitor spot survives (the WM
        # re-clamps if that monitor is now gone).
        try:
            self.root.geometry(s0.get("main_geometry") or "1280x800")
        except tk.TclError:
            self.root.geometry("1280x800")
        self.root.configure(bg="#1a1a1a")
        if s0.get("main_zoomed"):
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass
        s = self.profiles_data.setdefault("settings", {})
        fam = s.get("font_family",
                    "Consolas" if sys.platform == "win32" else
                    "Monospace")
        size = s.get("font_size", 11)
        self.base_font = tkfont.Font(family=fam, size=size)
        self.bold_font = tkfont.Font(family=fam, size=size,
                                     weight="bold")
        self.small_font = tkfont.Font(family=fam, size=size)
        self.small_bold = tkfont.Font(family=fam, size=size,
                                      weight="bold")

        menubar = tk.Menu(self.root)
        m_set = tk.Menu(menubar, tearoff=0)
        m_set.add_command(label="Font...", command=self.font_dialog)
        m_set.add_separator()
        m_set.add_command(label="Bigger   Ctrl+=",
                          command=lambda: self.bump_font(+1))
        m_set.add_command(label="Smaller  Ctrl+-",
                          command=lambda: self.bump_font(-1))
        menubar.add_cascade(label="Settings", menu=m_set)
        m_tools = tk.Menu(menubar, tearoff=0)
        m_tools.add_command(label="Aliases && Triggers...",
                            command=lambda: dialogs.BuilderDialog(self))
        m_tools.add_command(label="Keybindings...",
                            command=lambda: dialogs.KeybindDialog(self))
        if self.guild.lower() == "vikings":
            m_tools.add_command(label="Viking Status...",
                                command=self.open_viking_status)
        m_tools.add_separator()
        m_tools.add_command(label="Reload cascade",
                            command=self.reload_cascade)
        menubar.add_cascade(label="Tools", menu=m_tools)
        self.root.config(menu=menubar)
        self.root.bind("<Control-equal>", lambda e: self.bump_font(+1))
        self.root.bind("<Control-plus>", lambda e: self.bump_font(+1))
        self.root.bind("<Control-minus>", lambda e: self.bump_font(-1))

        # --- top bar ---
        self.topbar = tk.Frame(self.root, bg="#222236")
        self.tb = {}
        for key, init in [("uptime", "up ?"), ("reboot", ""),
                          ("room", "?"), ("rounds", "rnd 0"),
                          ("idle", "idle 0:00"), ("deadman", "DM ok")]:
            lbl = tk.Label(self.topbar, text=init, bg="#222236",
                           fg="#99aacc", font=self.small_font, padx=10)
            lbl.pack(side="left")
            self.tb[key] = lbl
        self.tb["deadman"].configure(fg="#66cc66")
        self.tb["room"].configure(fg="#ccccff")

        # --- main split ---
        self.paned = tk.PanedWindow(self.root, orient="horizontal",
                                    bg="#1a1a1a", sashwidth=5)
        self.text = tk.Text(self.paned, bg="#101014", fg="#cccccc",
                            font=self.base_font, wrap="word",
                            state="disabled", padx=8, pady=6,
                            insertbackground="#cccccc",
                            selectbackground="#334466")
        right = self.right_pane = tk.PanedWindow(self.paned, orient="vertical",
                                                 bg="#1a1a1a", sashwidth=5)
        self.chat = tk.Text(right, bg="#14101a", fg="#ccaadd",
                            font=self.small_font, wrap="word",
                            state="disabled", padx=6, pady=4, height=14)
        self.map = MapPane(right, self.click_exit,
                           walk_cb=self.walk_to_room,
                           width=300, height=260,
                           fonts={"room": self.small_bold,
                                  "exit": self.small_bold,
                                  "trail": self.small_font})
        self.info = tk.Text(right, bg="#10141a", fg="#88aabb",
                            font=self.small_font, wrap="word",
                            state="disabled", padx=6, pady=4, height=8)
        # Tracked-item highlights (necro Track): dim-red bg when a count is
        # below its low threshold; a faint header for the section.
        self.info.tag_configure("track_low", background="#3a1414",
                                foreground="#ffb0b0")
        self.info.tag_configure("track_hdr", foreground="#cc9933")
        self.info.tag_configure("necro_ok", foreground="#46c246")
        self.info.tag_configure("necro_bad", foreground="#e0703a")
        right.add(self.chat, minsize=80)
        right.add(self.map, minsize=100, height=240)
        right.add(self.info, minsize=60)
        self.paned.add(self.text, minsize=400)
        self.paned.add(right, minsize=220, width=320)

        self.vit_frame = tk.Frame(self.root, bg="#15151c")
        self.bars = []
        self.build_vitals()

        self.status = tk.Label(self.root, text="disconnected",
                               anchor="w", bg="#222230", fg="#8899aa",
                               font=self.small_font, padx=8)
        self.entry = tk.Entry(self.root, bg="#1e1e26", fg="#eeeeee",
                              font=self.base_font,
                              insertbackground="#eeeeee", relief="flat")
        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<Up>", self.history_up)
        self.entry.bind("<Down>", self.history_down)
        # Numpad must be intercepted at the WIDGET level: Tk runs
        # widget-class bindings before toplevel bindings, so a
        # root-level "break" is too late.
        self.entry.bind("<KeyPress>", self.on_keypress)
        self.root.bind("<KeyPress>", self.on_keypress)
        self.root.bind("<Escape>", self.handle_escape)
        self.root.bind("<Prior>", lambda e: self.page_output(-1))
        self.root.bind("<Next>", lambda e: self.page_output(+1))

        self.topbar.pack(side="top", fill="x")
        self.entry.pack(side="bottom", fill="x", padx=2, pady=2)
        self.status.pack(side="bottom", fill="x")
        self.vit_frame.pack(side="bottom", fill="x")
        self.paned.pack(side="top", fill="both", expand=True)
        self.root.after(200, self.restore_pane_sashes)  # once panes are sized
        self.entry.focus_set()
        self.text.bind("<Button-1>", self.click_press)
        self.text.bind("<B1-Motion>", self.click_drag)
        self.text.bind("<ButtonRelease-1>", self.click_release)
        self.text.bind("<Button-3>", self.output_context_menu)

        for w in self.cascade.warnings:
            self.write_local(f"[config] {w}", "#cc9933")
        for e in self.cascade.errors:
            self.write_local(f"[config] ERROR: {e}", "#cc6666")

    def reload_cascade(self):
        self.cascade.reload()
        self.load_layers()
        self.build_vitals()
        self.write_local("[cascade reloaded]", "#66cc66")
        for w in self.cascade.warnings:
            self.write_local(f"[config] {w}", "#cc9933")
        for e in self.cascade.errors:
            self.write_local(f"[config] ERROR: {e}", "#cc6666")

    # ------------------------------------------------ guild swap (Gswap)
    # Same character, same connection, swapped guild object (3k 'gswap').
    # The cascade's guild layer is retargeted and reloaded live - every
    # `self.guild`-gated branch (guild chat alias, harvest trigger, vitals,
    # necroguard) re-points itself off the new string. No relaunch.
    GUILD_ALIASES = {
        "blade": "bladesingers", "bs": "bladesingers",
        "bladesinger": "bladesingers", "bladesingers": "bladesingers",
        "necro": "necromancers", "nec": "necromancers",
        "necromancer": "necromancers", "necromancers": "necromancers",
    }

    def _available_guilds(self):
        gdir = paths.guild_dir(self.mud)
        if not os.path.isdir(gdir):
            return []
        return sorted(f[:-5] for f in os.listdir(gdir)
                      if f.endswith(".json") and not f.startswith("_"))

    def cmd_gswap(self, arg):
        name = arg.strip().lower()
        avail = ", ".join(self._available_guilds()) or "(none)"
        if not name:
            self.write_local(
                f"Active guild: {self.guild}. Gswap <guild> to switch. "
                f"Available: {avail}", "#cc9933")
            return
        self.switch_guild(name)

    def switch_guild(self, name):
        canonical = self.GUILD_ALIASES.get(name.lower(), name.lower())
        if canonical == self.guild.lower():
            self.write_local(f"Already a {canonical}.", "#cc9933")
            return
        if not os.path.exists(paths.guild_file(self.mud, canonical)):
            avail = ", ".join(self._available_guilds()) or "(none)"
            self.write_local(
                f"No '{canonical}' guild layer for {self.mud}. "
                f"Available: {avail}", "#cc6666")
            return
        self.guild = canonical
        self.cascade.guild = canonical
        self.cascade.reload()
        self.load_layers()
        self.build_vitals()
        for w in self.cascade.warnings:
            self.write_local(f"[config] {w}", "#cc9933")
        for e in self.cascade.errors:
            self.write_local(f"[config] ERROR: {e}", "#cc6666")
        # Drop the outgoing guild's status leftovers so the info pane and
        # the blur/portal line don't show stale data under the new guild.
        self.blur_portal = ""
        self.last_deltas = ""
        for k in ("worth", "protection", "veil", "reset", "circle",
                  "corpses", "glamor", "tport", "tier"):
            self.vars.pop(k, None)
        self.update_info()
        self.root.title(f"KatMUD — {self.display_name} "
                        f"({self.guild}) {self.host}:{self.port}")
        self.write_local(f"[gswap → {canonical}]", "#66cc66")
        self._send_on_activate()

    def _send_on_activate(self):
        """Fire the active guild layer's `on_activate` batch to the MUD -
        the mud-side reflex/panic reconfiguration that must follow a gswap
        (and runs once at guild login so the default guild is set up too)."""
        cmds = list(self.cascade.get("on_activate", []) or [])
        if not cmds:
            return
        self.write_local(f"[guild activate: {len(cmds)} command(s)]",
                         "#557755")
        for c in cmds:
            self.send_line(c, manual=True)

    # ------------------------------------------------ vitals row
    def build_vitals(self):
        for child in self.vit_frame.winfo_children():
            child.destroy()
        self.bars = []
        for spec in self.vitals_cfg:
            bar = VitalsBar(self.vit_frame, spec.get("label", "?"),
                            spec.get("color", "#888888"),
                            width=spec.get("width", 190),
                            warn=spec.get("warn"),
                            font=self.small_bold)
            bar.pack(side="left", padx=4, pady=3)
            self.bars.append((spec, bar))
        self.update_vitals()

    def update_vitals(self):
        v = self.vitals
        for spec, bar in self.bars:
            if spec.get("enemy"):
                enemy = v.get("enemy", "")
                if len(enemy) > 1:
                    bar.label = enemy[:18]
                    bar.set(v.get("enemycond", 0), 100)
                else:
                    bar.label = spec.get("label", "Enemy")
                    bar.set(0, 0)
            else:
                cur_f = spec.get("cur", "")
                top = v.get(spec.get("max", ""), 0) or \
                    self.vitals_max.get(cur_f, 0)
                bar.set(v.get(cur_f, 0), top)

    # ------------------------------------------------ fonts
    def apply_fonts(self, family=None, size=None):
        s = self.profiles_data.setdefault("settings", {})
        if family:
            s["font_family"] = family
        if size:
            s["font_size"] = max(7, min(32, size))
        fam = s.get("font_family", self.base_font.cget("family"))
        size = s.get("font_size", self.base_font.cget("size"))
        self.base_font.configure(family=fam, size=size)
        self.bold_font.configure(family=fam, size=size)
        self.small_font.configure(family=fam, size=size)
        self.small_bold.configure(family=fam, size=size)
        bar_h = max(22, size * 2 + 6)
        for _spec, b in self.bars:
            b.h = bar_h
            b.configure(height=bar_h)
        self.update_vitals()
        self.map.redraw()
        profiles.save(self.profiles_data)

    def bump_font(self, delta):
        s = self.profiles_data.setdefault("settings", {})
        cur = s.get("font_size", self.base_font.cget("size"))
        self.apply_fonts(size=cur + delta)
        return "break"

    def font_dialog(self):
        dialogs.FontDialog(self)

    # ------------------------------------------------ keybindings
    def on_keypress(self, event):
        if event.state & 0x4:          # Ctrl held
            return None
        if not self.echo_on:
            self.last_manual = time.time()
            return None
        keysym = None
        if sys.platform == "win32" and event.keycode in \
                WIN_NUMPAD_KEYCODES:
            keysym = WIN_NUMPAD_KEYCODES[event.keycode]
        elif event.keysym in KP_KEYSYMS or event.keysym in self.keys:
            keysym = event.keysym
        if event.char or keysym:
            self.last_manual = time.time()
            if self.deadman_tripped:
                self.deadman_tripped = False
                self.write_local(
                    "*** Deadman released - traffic resumed ***",
                    "#66cc66")
        if keysym and keysym in self.keys:
            if keysym in KP_KEYSYMS and \
                    self.setting("numpad_only_when_empty", False) and \
                    self.entry.get():
                return None
            self.process_input(self.keys[keysym], manual=True)
            return "break"
        return None

    def click_exit(self, direction):
        self.last_manual = time.time()
        self.send_line(direction, manual=True)

    # ------------------------------------------------ text panes
    def ensure_tag(self, widget, tag):
        ident = (str(widget), tag)
        if ident in self._known_tags:
            return
        opts = tag_to_style(tag)
        kwargs = {}
        if "foreground" in opts:
            kwargs["foreground"] = opts["foreground"]
        if opts.get("background"):
            kwargs["background"] = opts["background"]
        if opts.get("bold"):
            kwargs["font"] = self.bold_font
        if opts.get("underline"):
            kwargs["underline"] = True
        widget.tag_configure(tag, **kwargs)
        self._known_tags.add(ident)

    def _web_record(self, buf, line):
        """Append to a dashboard ring buffer (spec: docs/superpowers/specs/
        2026-06-21-web-dashboard-design.md). buf is one of
        self._web_output/_web_chats/_web_tells; deque(maxlen=...) drops
        the oldest entry once full, so this never needs its own trim."""
        buf.append({"t": time.time(), "line": line})

    def write_spans(self, spans, newline=True, widget=None):
        widget = widget or self.text
        if widget is self.text:
            self._web_record(self._web_output,
                             "".join(chunk for chunk, _tag in spans))
        at_bottom = widget.yview()[1] > 0.999
        widget.configure(state="normal")
        for chunk, tag in spans:
            if tag:
                self.ensure_tag(widget, tag)
                widget.insert("end", chunk, tag)
            else:
                widget.insert("end", chunk)
        if newline:
            widget.insert("end", "\n")
        lines = int(widget.index("end-1c").split(".")[0])
        if lines > 5500:
            widget.delete("1.0", f"{lines - 5000}.0")
        widget.configure(state="disabled")
        if at_bottom and not (self.frozen and widget is self.text):
            widget.see("end")

    def write_local(self, msg, color="#8899aa", widget=None):
        widget = widget or self.text
        tag = f"local{color}"
        ident = (str(widget), tag)
        if ident not in self._known_tags:
            widget.tag_configure(tag, foreground=color)
            self._known_tags.add(ident)
        widget.configure(state="normal")
        widget.insert("end", msg + "\n", tag)
        widget.configure(state="disabled")
        if not (self.frozen and widget is self.text):
            widget.see("end")
        # Tee client-side narration into the open session log so a single #log
        # captures the [explore]/[chart]/[map]/Mapcheck decisions, #chartdebug
        # traces and the '> cmd' echo (none of which reach the text stream).
        # 'LOCAL ' prefix keeps them filterable: grep '^LOCAL '.
        if self.log_file:
            try:
                self.log_file.write("LOCAL " + msg + "\n")
            except (OSError, ValueError):
                pass

    def status_note(self, key, msg):
        """One-time warning in the lower-right info pane (spec 5.1)
        - never the main output, where combat spam buries it."""
        if key in self._warned_fields:
            return
        self._warned_fields.add(key)
        self.status_notes.append(msg)
        self.update_info()

    def set_status(self, msg):
        self.status_text = msg
        self.show_status()

    # ------------------------------------------------ freeze/scroll
    def click_press(self, _event=None):
        self._cancel_hold()
        self._hold_job = self.root.after(400, self.freeze_output)

    def click_drag(self, _event=None):
        self._cancel_hold()
        self.freeze_output()

    def click_release(self, _event=None):
        self._cancel_hold()
        if not self.frozen:
            self.entry.focus_set()

    def _cancel_hold(self):
        if self._hold_job:
            self.root.after_cancel(self._hold_job)
            self._hold_job = None

    def freeze_output(self, _event=None):
        self._hold_job = None
        if not self.frozen:
            self.frozen = True
            self.status.configure(
                text="PAUSED - select & Ctrl+C to copy, Esc to resume",
                fg="#ffcc55")

    def unfreeze_output(self):
        self.frozen = False
        self.status.configure(fg="#8899aa")
        self.show_status()
        self.text.see("end")
        self.entry.focus_set()

    def _stop_all_automation(self, why="stopped"):
        """Halt every auto-walker (Bot/Hunt/Run/Explore/Agent) and the
        script bot at once - the single kill switch behind both `#stop` and
        the Esc panic-stop. Returns True if anything was actually running."""
        self.cancel_walk()
        acted = False
        if self._bot_on:
            self._bot_stop(why); acted = True
        if self._run_on:
            self._run_stop(why); acted = True
        if self._explore_on:
            self._explore_stop(why); acted = True
        if self._agent_on:
            self._agent_stop(why); acted = True
        if self._macro_on:
            self._macro_stop(why); acted = True
        # also halt the script-based bot (katmud_scripts.py reads this var;
        # "off" is its documented stop) so this is one kill switch for every
        # automation style.
        if str(self.vars.get("bot", "")).strip().lower() not in (
                "", "0", "off"):
            self.vars["bot"] = "off"
            self.write_local("[script bot: stopped]", "#aa88cc")
            acted = True
        return acted

    def handle_escape(self, _event=None):
        if self.frozen:
            self.unfreeze_output()
            return "break"
        # Esc is also the panic kill switch for any running auto-walker: stop
        # it before touching the entry/chart buffer, so a long Explore (or any
        # automation) can always be interrupted with a single keypress.
        if self._bot_on or self._run_on or self._explore_on \
                or self._agent_on or self._macro_on:
            self._stop_all_automation("Esc")
            self.entry.delete(0, "end")
            return "break"
        # In charting mode, Esc drops the whole queued command buffer (and
        # any in-flight gate wait) before clearing the entry, so a runaway
        # paste can be stopped cold.
        if self.map_backend == "sqlite" and self._sql_charting \
                and (self._sql_chart_buf or not self._sql_gate_open):
            n = len(self._sql_chart_buf)
            self._sql_chart_buf.clear()
            self._sql_gate_cancel_timer()
            self._sql_gate_open = True
            self._sql_chart_from = self._sql_chart_move = None
            self.write_local(
                f"[chart] queue cleared ({n} command(s) dropped)",
                "#ffaa44")
            self.entry.delete(0, "end")
            return "break"
        self.entry.delete(0, "end")
        return None

    def page_output(self, direction):
        if not self.frozen:
            self.freeze_output()
        self.text.yview_scroll(direction, "pages")
        return "break"

    # ------------------------------------------------ status line
    def show_status(self):
        if self.frozen:
            return
        # Changelings put their FFF I-field (Flux/Density/FF/form) on the
        # status bar under the hpbars instead of the (redundant) connection
        # text - the connection state lives in the title bar + topbar anyway.
        if self.guild.lower() == "changelings" and self.changeling_status:
            self.status.configure(text=self.changeling_status)
        elif self.guild.lower() == "bladesingers" and self.blade_status:
            self.status.configure(text=self.blade_status)
        elif self.guild.lower() == "necromancers" and self.necro_status:
            self.status.configure(text=self.necro_status)
        elif self.stat1 or self.stat2:
            self.status.configure(text=f"{self.stat1}    {self.stat2}")
        elif self.viking_spells:
            self.status.configure(text=self.viking_spells)
        else:
            self.status.configure(text=self.status_text)

    BP_RE = re.compile(
        r"B:\s*(\d+)/(\d+)\s*\((\d+)%\)\s+P:\s*(\d+)/(\d+)\s*\((\d+)%\)")
    # 3k has no portal mechanic (unlike 3s) - its bladesinger status line is
    # one consolidated "[Mode:... Eff:... BL <n/m>(p%) G2N:...]" line instead
    # of the separate HP:/B:P: line 3s sends.
    BLADE_STATUS_RE = re.compile(
        r"Mode:(\S*)\s+Eff:(\S*)\s+BL\s*<(\d+)/(\d+)>\((\d+)%\)\s+G2N:(\S*)")

    def capture_statline(self, clean):
        changed = False
        if self.guild.lower() == "bladesingers" and clean.startswith("[Mode:"):
            self.blade_status = clean.strip().lstrip("[").rstrip("]").strip()
            changed = True
            m = self.BLADE_STATUS_RE.search(clean)
            if m:
                bn, bm, bp = int(m.group(3)), int(m.group(4)), int(m.group(5))
                self._estimate_blur_reset(bp)
                self._blur_measure_charge(bn)
                rt = (f"  reset ~{blade.fmt_secs(self.blur_reset_secs)}"
                      if self.blur_reset_secs else "")
                at = (f"  auto@{self.blur_auto_pct_eff}%"
                      if self.blur_auto and self.blur_auto_pct_eff is not None
                      else "")
                self.blur_portal = f"Blur: {bn}/{bm} ({bp}%)" + rt + at
                self.vars.update(blur_n=bn, blur_max=bm, blur_pct=bp,
                                 bp_t=time.time())
                self._maybe_auto_blur(bn, bm, bp)
                self.update_info()
        elif clean.startswith("[") and "] [" in clean:
            self.stat1 = clean.strip(); changed = True
        elif clean.startswith("G2N:"):
            self.stat2 = clean.strip(); changed = True
        elif clean.startswith("HP:"):
            m = self.BP_RE.search(clean)
            if m:
                bn, bm, bp, pn, pm, pp = (int(x) for x in m.groups())
                self._estimate_blur_reset(bp)
                self._blur_measure_charge(bn)
                rt = (f"  reset ~{blade.fmt_secs(self.blur_reset_secs)}"
                      if self.blur_reset_secs else "")
                at = (f"  auto@{self.blur_auto_pct_eff}%"
                      if self.blur_auto and self.blur_auto_pct_eff is not None
                      else "")
                self.blur_portal = (f"Blurs: {bn}/{bm} ({bp}%)  "
                                    f"Portals: {pn}/{pm} ({pp}%)" + rt + at)
                self.vars.update(blur_n=bn, blur_max=bm, blur_pct=bp,
                                 portal_n=pn, portal_max=pm,
                                 portal_pct=pp, bp_t=time.time())
                self._maybe_auto_blur(bn, bm, bp)
                self.update_info()
        if changed:
            self.show_status()
        return changed

    def _estimate_blur_reset(self, bp):
        """Estimate the wall-clock length of one full blur reset (0->100%)
        from the streaming B:n/m (bp%) samples. Anchor a (pct, time) at the
        start of a cycle; once bp has climbed, extrapolate the full 100%
        time. A drop in bp marks a completed/restarted cycle -> re-anchor.
        The learned value is persisted so it survives relogs."""
        now = time.time()
        anchor = self._blur_anchor
        if anchor is None or bp < anchor[0]:
            # first sample, or the cycle wrapped (refill) -> start fresh
            self._blur_anchor = (bp, now)
            return
        dp = bp - anchor[0]
        dt = now - anchor[1]
        if dp >= 3 and dt > 0:           # enough movement to be meaningful
            est = int(dt / (dp / 100.0))
            prev = self.blur_reset_secs
            self.blur_reset_secs = est
            # persist only on a meaningful change (>30s) to avoid writing
            # the config on every combat prompt
            if prev is None or abs(est - prev) > 30:
                self.set_setting("blur_reset_secs", est)

    def _blur_measure_charge(self, bn):
        """Learn the wall-clock lifetime of ONE blur charge while fighting -
        the interval between two successive B-count drops (each drop = a charge
        spent in combat). Only fight-time intervals count: a refill (count
        rises) or a steady count re-anchors/holds without measuring, so idle
        time at full charges never inflates the estimate. EMA-smoothed +
        persisted so the kickoff threshold survives relogs."""
        now = time.time()
        last = self._blur_last_drop
        if last is None or bn > last[0]:        # first sample or refill -> anchor
            self._blur_last_drop = (bn, now)
            return
        if bn < last[0]:                        # a charge was consumed
            interval = (now - last[1]) / (last[0] - bn)
            self._blur_last_drop = (bn, now)
            if last[1] and interval > 0:
                prev = self.blur_secs_per_charge
                est = interval if prev is None else 0.5 * prev + 0.5 * interval
                self.blur_secs_per_charge = est
                if prev is None or abs(est - prev) > 2:
                    self.set_setting("blur_secs_per_charge", int(est))

    def _blur_threshold(self, bm):
        """Reset% at which to fire the kickoff blur. Manual `blur_auto_pct`
        (>0) wins; otherwise derive from the time to burn all `bm` charges vs
        the full reset wall time, backed off by `blur_auto_margin_pct`. Returns
        None when there isn't enough learned data yet (no fire)."""
        manual = self.setting("blur_auto_pct", 0) or 0
        if manual > 0:
            return max(1, min(99, int(manual)))
        per = self.setting("blur_duration_secs", 0) or self.blur_secs_per_charge
        if not (self.blur_reset_secs and per):
            return None
        margin = self.setting("blur_auto_margin_pct", 5) or 0
        pct = 100.0 * (1 - (bm * per) / self.blur_reset_secs) - margin
        return max(5, min(95, int(round(pct))))

    def _maybe_auto_blur(self, bn, bm, bp):
        """Kickoff the blur chain late in a reset cycle so every charge is spent
        before the refill wastes it. Fires once when still at MAX charges and
        reset% has reached the computed threshold; the fade trigger re-casts
        through the remaining charges. Gated by the `blur` Toggle + a cooldown
        (the B count lags the send by a round). send_line honours deadman."""
        if not self.blur_auto or self.guild.lower() != "bladesingers":
            self.blur_auto_pct_eff = None
            return
        thr = self._blur_threshold(bm)
        self.blur_auto_pct_eff = thr
        if thr is None or bn < bm or bp < thr:
            return
        if time.time() - self._blur_auto_last < 15:    # don't double-fire
            return
        self._blur_auto_last = time.time()
        self.write_local(
            f"[blur: kickoff at {bp}% reset ({bm} charges to spend)]",
            "#cc66ff")
        self.send_line(self.setting("blur_command", "blur"))

    # ------------------------------------------------ networking
    def connect(self):
        if self.conn and self.conn.alive:
            self.write_local("Already connected. #disconnect first.")
            return
        self.mip_sent = False
        self.password_sent = False
        self._logged_in = False
        self.guild_login_sent = False
        self.conn = MudConnection(self.host, self.port, self.queue,
                                  self.mip_pin)
        self.conn.start()

    def disconnect(self):
        if self.conn:
            self.conn.stop()

    # ------------------------------------------------ credentials
    def get_login_password(self, rejected=False):
        """Stored password, or prompt. Returns (password_or_None,
        was_prompted). On rejection: re-prompt naming the key
        explicitly (spec 3)."""
        if not rejected:
            pw = credentials.get_password(self.mud, self.character)
            if pw:
                return pw, False
        key = credentials.key_label(self.mud, self.character)
        if rejected:
            msg = (f"Stored password for {self.mud}/{self.character} "
                   f"was rejected.\nEnter new password (stored as "
                   f"{key} on success),\nor Cancel to type it "
                   "yourself:")
        else:
            extra = ("" if credentials.available() else
                     f"\n\nNOTE: {credentials.UNAVAILABLE_MSG}")
            msg = (f"No stored password for {self.mud}/"
                   f"{self.character}.\nEnter it to log in (stored as "
                   f"{key} on success),\nor Cancel to type it "
                   f"yourself.{extra}")
        pw = simpledialog.askstring("KatMUD password", msg,
                                    parent=self.root, show="*")
        return pw, True

    def confirm_login(self):
        """First vitals packet = actually logged in. Store a prompted
        password now (spec 3: store on success)."""
        if self._logged_in:
            return
        self._logged_in = True
        if self._pending_store_pw and credentials.available():
            err = credentials.set_password(self.mud, self.character,
                                           self._pending_store_pw)
            self.write_local(
                "[password stored in credential manager]"
                if not err else f"[password store failed: {err}]",
                "#557755" if not err else "#cc6666")
        self._pending_store_pw = None

    AUTH_FAIL_RE = re.compile(r"wrong password|incorrect password|"
                              r"password incorrect", re.IGNORECASE)

    def check_auth_failure(self, clean):
        if not self.password_sent or self._logged_in:
            return
        if self.AUTH_FAIL_RE.search(clean):
            self.password_sent = False
            self._pending_store_pw = None
            pw, prompted = self.get_login_password(rejected=True)
            if pw:
                self.password_sent = True
                self._pending_store_pw = pw if prompted else None
                self.conn.send(pw)
                self.write_local("[password re-sent]", "#557755")

    # ------------------------------------------------ map support
    def start_map_loader(self):
        if self.map_backend == "sqlite":
            self.start_sql_map()
            return
        mpath = paths.map_file(self.mud)
        spath = paths.speedruns_file(self.mud)
        legacy = os.path.join(paths.mud_dir(self.mud),
                              "speedruns.tin")
        if os.path.exists(legacy):
            self.write_local(
                "[map] NOTE: legacy speedruns.tin present but "
                f"IGNORED - this build loads {os.path.basename(spath)}",
                "#cc9933")
        patches, perrs = mapdata.collect_patches(self.cascade,
                                                 self.mud)
        for e in perrs:
            self.write_local(f"[map] patch file: {e}", "#cc6666")

        def loader():
            try:
                tmap = mapdata.TinMap(mpath, patches=patches)
                sr = {}
                if os.path.exists(spath):
                    sr = mapdata.load_speedruns(spath)
                self.queue.put(("maploaded", tmap, sr))
            except FileNotFoundError:
                self.queue.put(("maploaded", None,
                                f"map file not found: {mpath}"))
            except Exception as e:
                self.queue.put(("maploaded", None,
                                f"map load error: {e}"))

        if os.path.exists(mpath):
            threading.Thread(target=loader, daemon=True).start()
        else:
            self.write_local(f"[map] no map file: {mpath}", "#cc9933")

    # ============================================= sqlite backend (4)
    # Auto-charting for 3s. On every room change the MUD sends a BAD
    # (room) and DDD (exits) MIP packet; both carry the room vnum, our
    # canonical key. We buffer the pair, reconcile via mapparse, and
    # chart any room we haven't seen. Pathfinding / go / visual render
    # come in later stages - this stage only builds the database.
    def start_sql_map(self):
        path = paths.map_db_file(self.mud)
        try:
            self.mapdb = mapsql.MapDB(path)
        except Exception as e:                       # noqa: BLE001
            self.write_local(f"[map] sqlite open failed: {e}", "#cc6666")
            self.mapdb = None
            return
        self.write_local(
            f"[map] sqlite backend: {self.mapdb.room_count()} rooms in "
            f"{os.path.basename(path)}", "#66cc66")
        # Pre-learn the high part of every full (6+ digit) charted room so
        # truncated DDD ids resolve to the full room from the first packet.
        self._sql_vnum_hi = {}
        self._sql_trunc = {}
        for r in self.mapdb.conn.execute(
                "SELECT vnum FROM rooms WHERE vnum > 99999"):
            self._sql_learn_vnum(r["vnum"])
        self.sql_set_status()

    # ---- packet intake -------------------------------------------------
    # FOLLOWING mode reads location straight off DDD (it carries the vnum
    # and also fires on every look/glance, giving free re-pins); BAD just
    # refreshes the displayed room name. CHARTING mode ignores DDD as a
    # location source and waits for BAD - the authoritative movement
    # signal - then charts the room behind the lockstep gate.
    # ---- 3s 5-digit tilde truncation (see mapparse.is_truncated_id) ----
    def _sql_learn_vnum(self, vnum):
        """Record a full (6+ digit) id's high part keyed by its low 5, so a
        later truncated DDD id can be lifted back to the full id."""
        if vnum is not None and vnum > 99999:
            self._sql_vnum_hi[vnum % 100000] = vnum - (vnum % 100000)

    def _sql_note_trunc(self, tilde, bracket):
        """Record an OBSERVED tilde-truncation (a BAD bracket that reveals the
        full id) so a later bracket-less DDD carrying the same truncated tilde
        resolves to the full id. Learned only from real packets - never
        derived speculatively - so a genuine low-numbered room is never
        wrongly lifted."""
        if mapparse.is_truncated_id(tilde, bracket):
            self._sql_trunc[tilde] = bracket
            self._sql_learn_vnum(bracket)

    def _sql_resolve_low5(self, vnum):
        """Lift a (possibly truncated) wire id to its full id. First an
        OBSERVED truncated-tilde -> full mapping (any width, learned from a
        real bracket mismatch); else the learned high part for its low 5
        (6-digit rooms). Idempotent on ids already full; unknown ids pass
        through unchanged. NOTE: a learned low-5 collision could lift a
        genuine <100000 room - acceptable on 3s (the truncated ids ARE the
        6-digit rooms; no real rooms clash in that range)."""
        if vnum is None:
            return None
        full = self._sql_trunc.get(vnum)
        if full is not None:
            return full
        hi = self._sql_vnum_hi.get(vnum % 100000)
        return hi + (vnum % 100000) if hi else vnum

    def _sql_canon_vnum(self, tilde, bracket=None):
        """Canonical full id for a wire room id. A bracket that reveals
        truncation gives the full id directly (and is learned); otherwise we
        fall back to the learned/charted resolution."""
        if mapparse.is_truncated_id(tilde, bracket):
            self._sql_note_trunc(tilde, bracket)
            return bracket
        return self._sql_resolve_low5(tilde)

    def sql_on_bad(self, data):
        self._sql_bad = data
        if self._sql_charting:
            self.sql_reconcile_chart()
        else:
            self.sql_follow_bad(data)

    def sql_on_ddd(self, data):
        self._sql_ddd = data
        if self._sql_charting:
            self.sql_reconcile_chart()
        else:
            self.sql_follow_ddd(mapparse.parse_exits(data))

    # ---- following: DDD is the location authority ----
    def sql_follow_ddd(self, pe):
        """A DDD packet in following mode: its trailing vnum is the proven
        current room, so we just move '@' there. We never predict or
        chart - on a fast run '@' may lag the player by ~1s then catch up
        packet by packet, but it can't be lost (the next DDD re-pins) or
        mis-charted (nothing is written). An uncharted vnum means the
        player has walked off the known map; the renderer flags it - use
        Chart to map onward."""
        if pe.vnum is None:
            return
        vnum = self._sql_resolve_low5(pe.vnum)   # DDD has no bracket: lift it
        moved = (self._sql_cur_vnum is not None and vnum != self._sql_cur_vnum)
        self._sql_cur_vnum = vnum
        self.room_vnum = vnum
        # self.exits must be settled before _chaossea_on_room_packet runs -
        # it synchronously captures self.exits into _chaossea_room_exits, so
        # calling it first (as before) always recorded the PREVIOUS room's
        # exit list under the new room's fake id. The (expensive) map render
        # still happens after, so neither bot waits on it.
        if pe.exits:
            self.exits = pe.exits
        elif moved:
            # An exits-less DDD with an UNCHANGED vnum is usually a
            # same-room glance, and keeping the old list is correct then.
            # But the vnum here just changed, so this is NOT a glance at
            # the room we're standing in - these are stale exits from the
            # room we just left. Treat them as unknown rather than handing
            # a bot a neighbor's exit list (caused a live Chaossea move
            # into a nonexistent 'out' exit - see logs/normal-20260623.log).
            # (This vnum-change check is itself blind inside a zone that
            # collapses every room to one vnum, e.g. the Sea of Chaos -
            # see _chaossea_exits_unknown_retry for that case.)
            self.exits = []
        self._bot_on_room_packet()
        self._chaossea_on_room_packet(bool(pe.exits))
        if pe.exits:
            self.map.update_room(exits=pe.exits)
        self.sql_set_status()
        self.update_info()

    def sql_follow_bad(self, data):
        """A BAD packet in following mode: refresh the displayed room
        name only (location comes from DDD). Charting is off - nothing is
        written to the map."""
        pr = mapparse.parse_room(data)
        if pr is None:
            return
        self.room = pr.short_desc or self.room
        # BAD carries the bracket: learn the full id and show it (location
        # itself still comes from DDD).
        self.room_vnum = self._sql_canon_vnum(pr.vnum, pr.bracket_vnum)
        self.map.update_room(room=pr.short_desc)
        self.update_info()

    # ---- charting: BAD-gated, one room at a time ----
    def sql_reconcile_chart(self):
        """Pair the BAD + DDD of a single gated move into one observation
        and chart it. Pairing is by the vnum the packets REPORT (a lone
        glance DDD must never shift an arrival-order pairing); we act only
        once both agree on the room."""
        # A lone DDD (no BAD pending) whose id is the room we're standing in
        # is a glance/scan/look refresh, not a move: 3s sends BAD *before*
        # DDD on a real room change, and `look` never re-emits BAD for the
        # current room, so a DDD that arrives with no BAD and the current
        # vnum can only be an in-place refresh. Release the gate at once
        # instead of stalling the full no-BAD window - that ~2.5s wait on
        # every glance was the bulk of the charting slog.
        if (self._sql_ddd is not None and self._sql_bad is None
                and self._sql_chart_move is not None):
            pe = mapparse.parse_exits(self._sql_ddd)
            if (pe.vnum is not None
                    and self._sql_resolve_low5(pe.vnum)
                    == self._sql_chart_from):
                if pe.exits:
                    self.exits = pe.exits
                    self.map.update_room(exits=pe.exits)
                self._sql_ddd = None
                self._sql_chart_dbg(
                    f"refresh in {self._sql_chart_from} "
                    f"('{self._sql_chart_move}') - no move, gate released")
                self._sql_chart_from = self._sql_chart_move = None
                self._sql_gate_release()
                return
        if self._sql_bad is None or self._sql_ddd is None:
            return
        pr = mapparse.parse_room(self._sql_bad)
        if pr is None:                      # BAD with no id: unusable
            self._sql_bad = None
            return
        # charting doesn't go through sql_follow_bad, so learn the bracket's
        # truncation here too - it's how the matching DDD (no bracket) resolves.
        self._sql_note_trunc(pr.vnum, pr.bracket_vnum)
        pe = mapparse.parse_exits(self._sql_ddd)
        if pe.vnum is not None and pe.vnum != pr.vnum:
            self._sql_chart_dbg(f"BAD ~{pr.vnum} / DDD ~{pe.vnum} disagree "
                                "- waiting for the pair to settle")
            return                          # different rooms; wait to catch up
        bad, ddd = self._sql_bad, self._sql_ddd
        self._sql_bad = self._sql_ddd = None
        obs = mapparse.parse_packets(bad, ddd)
        if obs is None:
            return
        self.room = obs.short_desc or self.room
        self.exits = obs.exits
        self.room_vnum = obs.vnum
        self.map.update_room(room=obs.short_desc, exits=obs.exits)
        for w in obs.warnings:
            self.write_local(f"[map] {w}", "#cc9933")
        if self.mapdb is not None:
            self._sql_chart_arrival(obs)
        self._last_move = None
        self.update_info()

    def _sql_chart_arrival(self, obs):
        """A gated command produced a room (BAD = movement confirmed).
        Chart the room and the edge that led here from _sql_chart_from,
        then reopen the gate for the next queued command. A BRAND-NEW room
        defers to `rating` (area/coder + [House] detection) and the gate
        stays shut until sql_finish_room reopens it - 'until the new room
        is charted' is literal."""
        self._sql_gate_cancel_timer()       # BAD arrived: no fallback needed
        frm, via = self._sql_chart_from, self._sql_chart_move
        self._sql_chart_from = self._sql_chart_move = None
        if frm is None and self._sql_late_move is not None:
            # This packet is the late arrival of a move whose no-BAD window
            # already elapsed (and no newer move has pumped since). Chart it
            # off the stashed (from, move) so the edge isn't lost - but only
            # when the stashed command is movement-like (a compass dir or a
            # listed exit of the from-room), so a timed-out NON-move (e.g.
            # 'kill dragon') followed by some other room change can't fabricate
            # a junk exit named after it.
            sfrm, svia = self._sql_late_move
            if sfrm is not None and (
                    svia in self.COMPASS_DIRS
                    or (self.mapdb is not None
                        and svia in self.mapdb.exit_dirs(sfrm))):
                frm, via = sfrm, svia
                self._sql_chart_dbg(f"late arrival - recovered move '{via}' "
                                    f"from {frm}")
        self._sql_late_move = None
        self._sql_cur_vnum = obs.vnum       # full id (parse_packets recovered)
        self._sql_learn_vnum(obs.vnum)
        self._sql_migrate_truncated(obs)
        self._sql_chart_dbg(
            f"arrival {obs.vnum} via '{via}' from {frm} "
            f"({'known' if self.mapdb.has_room(obs.vnum) else 'NEW'})")

        if frm == obs.vnum:                 # didn't actually move (refresh)
            self.sql_set_status()
            self._sql_gate_release()
            return
        if self.mapdb.has_room(obs.vnum):   # known room: (re)link + move on
            self._sql_in_house = False
            self._sql_chart(frm, via, obs.vnum)
            row = self.mapdb.get_room(obs.vnum)
            if row is not None and row["area_id"] is None:
                # known but UNRATED (a charting miss / legacy row): the area
                # never heals on its own because known rooms aren't re-rated.
                # Re-rate now to backfill it - so walking/Exploring an area in
                # Chart mode fills in every missing area_id. Gate stays shut
                # until sql_finish_room reopens it (pend stays None -> the
                # refresh path just sets the area).
                self.sql_set_status()
                self.sql_send_rating()
                return
            self.sql_set_status()
            self._sql_gate_release()
            return
        # new room: hold it until rating reveals area + house status; the
        # gate stays closed until sql_finish_room finishes (or times out).
        self._sql_pending = {"obs": obs, "from": frm, "via": via}
        self.sql_send_rating()

    def _sql_migrate_truncated(self, obs):
        """If this room was previously charted under a truncated id (before we
        learned to recover the full id from the bracket), re-key that old row
        to the full id so revisiting heals the data. Tries both truncation
        shapes - low-5 (6-digit rooms) and leading-digit-dropped (the live
        [1774]~774 case) - guarded by a short-desc match so two genuinely
        different rooms sharing a truncated form are never merged."""
        full = obs.vnum
        if self.mapdb.has_room(full):
            return
        s = str(full)
        twins = {full % 100000}                 # 6-digit low-5 (legacy heal)
        # The leading-digit-dropped twin ([1774]~774) is only attempted when
        # we ACTUALLY observed this id truncated on the wire - otherwise a
        # genuine low-numbered room sharing a short_desc (maze rooms!) could
        # be wrongly merged.
        if full in self._sql_trunc.values() and len(s) > 1:
            twins.add(int(s[1:]))
        for twin in twins:
            if twin == full or twin <= 0:
                continue
            trow = self.mapdb.get_room(twin)
            if trow is not None and trow["short_desc"] == obs.short_desc:
                self.mapdb.migrate_vnum(twin, full)
                self.write_local(
                    f"[map] healed truncated room {twin} -> {full}", "#88aacc")
                return

    def _sql_chart(self, from_v, via, dst):
        """Link from_v --via--> dst for a CONFIRMED traversal. `via` must
        be a LISTED exit of from_v - we never fabricate an exit a room
        doesn't advertise (that bug created phantom edges like 34 nw->33).
        link_exit overwrites to_vnum, so this also HEALS a mis-charted edge
        and fills a special exit's destination (e.g. nw='climb over logs')
        while preserving its command columns.

        `via` may be the canonical direction, the literal command of an
        existing special exit (mapped back to that exit's direction), OR a
        direction the room never advertised - a hidden exit we just walked.
        The charting gate confirms one move from_v --via--> dst at a time,
        so recording even an unlisted exit can't fabricate a phantom; that
        is why this no longer bails when `via` isn't already listed (it was
        silently dropping edges into known rooms that lacked a stub)."""
        if from_v is None or not via or from_v == dst:
            return
        if not self.mapdb.has_room(from_v):
            self._sql_chart_dbg(
                f"edge {from_v} -{via}-> {dst} DROPPED (from-room uncharted)")
            return
        direction = via
        if direction not in self.mapdb.exit_dirs(from_v):
            for e in self.mapdb.iter_exits(from_v):
                if (e["command"] or "").lower() == via:
                    direction = e["direction"]
                    break
        self.mapdb.link_exit(from_v, direction, dst)
        self._sql_chart_dbg(f"linked {from_v} -{direction}-> {dst}")

    # ---- charting lockstep gate ----
    # No-BAD release window. A command that produced no room packet by now
    # didn't move us (chat, 'open door', a wall bump), so we stop waiting and
    # send the next one. This MUST exceed worst-case room-packet latency:
    # live 3s traces show a move's BAD can land >1.2s after the command
    # (dragon rooms etc.), so a shorter window fires mid-move and the late
    # packet can't chart the edge. 2.5s is the proven-safe value. The
    # _sql_late_move stash (see _sql_gate_timeout) is the backstop for the
    # rarer spike that still overruns this. Tunable via chart_nobad_ms.
    _SQL_CHART_NOBAD_MS = 2500

    def _sql_pump(self):
        """Send the next queued command if the gate is open. Records the
        room we send FROM and the command itself so the matching BAD can
        chart the edge, clears the packet buffers so only THIS arrival's
        BAD/DDD pair, and arms the no-BAD fallback timer."""
        if not self._sql_gate_open or not self._sql_chart_buf:
            return
        # Never release a queued charting move into active combat: hold the
        # buffer and retry once the fight ends, so the bot fights where it
        # stands instead of walking out mid-combat (autocombat + the kill
        # trigger resolve the fight; the next move waits). A single guarded
        # retry avoids stacking timers.
        if self.in_combat:
            if self._sql_combat_hold_job is None:
                self._sql_combat_hold_job = self.root.after(
                    400, self._sql_pump_retry)
            return
        cmd, manual = self._sql_chart_buf.popleft()
        self._sql_gate_open = False
        self._sql_chart_from = self._sql_cur_vnum
        self._sql_chart_move = cmd.strip().lower()
        self._sql_bad = self._sql_ddd = None
        # A fresh move supersedes any timed-out one: its late packet would
        # now be ambiguous, so drop the stash rather than risk mischarting.
        self._sql_late_move = None
        self._sql_chart_dbg(f"pump '{self._sql_chart_move}' "
                            f"from {self._sql_chart_from}")
        self._raw_send(cmd, manual)
        nobad = max(500, int(self.setting("chart_nobad_ms",
                                          self._SQL_CHART_NOBAD_MS)))
        self._sql_gate_job = self.root.after(nobad, self._sql_gate_timeout)

    def _sql_pump_retry(self):
        """Combat-hold retry: combat paused the gate; try to pump again."""
        self._sql_combat_hold_job = None
        self._sql_pump()

    def _sql_gate_timeout(self):
        """The no-BAD window elapsed. The command MIGHT be a non-move (no
        room packet coming) or a real move whose BAD is merely late. We
        can't tell yet, so we release the gate to stay responsive but STASH
        the in-flight (from, move): if its BAD/DDD then arrive before the
        next command pumps, the arrival still charts the edge instead of
        dropping it (the bug that left exits NULL when a move's packet
        overran this window). A fresh pump clears the stash."""
        self._sql_gate_job = None
        if self._sql_chart_from is not None and self._sql_chart_move:
            self._sql_late_move = (self._sql_chart_from, self._sql_chart_move)
            self._sql_chart_dbg(
                f"no-BAD timeout after '{self._sql_chart_move}' from "
                f"{self._sql_chart_from} - released; will still chart it if "
                "its packet arrives before the next move")
        self._sql_chart_from = self._sql_chart_move = None
        self._sql_gate_release()

    def _sql_gate_cancel_timer(self):
        if self._sql_gate_job is not None:
            self.root.after_cancel(self._sql_gate_job)
            self._sql_gate_job = None

    def _sql_gate_release(self):
        """Reopen the gate and let the next queued command through."""
        self._sql_gate_cancel_timer()
        self._sql_gate_open = True
        self._sql_pump()

    def sql_set_chart_mode(self, on, announce=True):
        """Enter/leave charting (map-building) mode. Entering arms the
        lockstep gate; leaving drops any queued commands (like Esc) and
        returns to free-running following. The map pane recolours via the
        `mapping` flag so the mode is always visible. announce=False
        suppresses the verbose banner (used by the combat auto-pause, which
        prints its own concise note)."""
        if on and not self._sql_charting:
            self._sql_charting = True
            self._sql_gate_open = True
            if announce:
                self.write_local(
                    "[CHART ON - building the map: one move at a time, gated "
                    "on the room packet. Esc clears the queue; Chart again to "
                    "leave.]", "#ffaa44")
            self._sql_chart_current_room()
        elif not on and self._sql_charting:
            n = len(self._sql_chart_buf)
            self._sql_chart_buf.clear()
            self._sql_gate_cancel_timer()
            if self._sql_combat_hold_job is not None:
                self.root.after_cancel(self._sql_combat_hold_job)
                self._sql_combat_hold_job = None
            if self._sql_rating_job is not None:
                self.root.after_cancel(self._sql_rating_job)
                self._sql_rating_job = None
            self._sql_pending = None
            self._sql_late_move = None
            self._sql_gate_open = True
            self._sql_chart_from = self._sql_chart_move = None
            self._sql_charting = False
            if announce:
                self.write_local(
                    "[CHART OFF - following mode]"
                    + (f" ({n} queued command(s) dropped)" if n else ""),
                    "#44aaff")
        self.sql_set_status()

    def _sql_chart_dbg(self, msg):
        """#chartdebug trace - shows what the lockstep gate is doing so a
        wrong-room edge or a stall can be seen as it happens."""
        if self._sql_chart_debug:
            self.write_local(f"[chart] {msg}", "#8899bb")

    def _sql_chart_current_room(self):
        """Entering Chart mode: make sure the room we're STANDING in is on
        the map before the first move, so that move's outbound exit has a
        charted from-room to attach to. Without this the first exit out of
        an uncharted room is silently dropped (the has_room guard in
        _sql_chart) and only reappears later as a reverse edge - the 'take
        it, reverse it, repeat 2-3x' slog, and the source of edges that
        looked assigned to the wrong room.

        A `look` does NOT re-emit BAD for the current room on 3s (BAD only
        fires on a real room change), so we can't wait for a packet - we
        synthesise the observation from the live following-mode state
        (self.room / self.exits / current vnum, all kept fresh by the DDD/
        BAD follow handlers) and run it through the normal new-room rating
        flow. Already-charted rooms need nothing."""
        v = self._sql_cur_vnum
        if v is None or self.mapdb is None or self.mapdb.has_room(v):
            return
        obs = mapparse.RoomObservation(
            vnum=v, exits=list(self.exits or []),
            short_desc=self.room if self.room not in ("", "?") else "",
            warnings=[])
        self._sql_chart_dbg(f"charting start room {v} before first move")
        self._sql_gate_open = False         # hold moves until it's charted
        self._sql_pending = {"obs": obs, "from": None, "via": None}
        self.sql_send_rating()

    def sql_send_rating(self):
        """Auto-send `rating` for a newly entered room and arm the
        swallow window. Sent raw (not via send_line) so it never
        becomes a bogus _last_move.

        Also arms a HARD fallback timer: rating completion otherwise rides
        only on incoming MUD text (sql_rating_capture), so a `rating` reply
        that never parses - or after which no text happens to arrive - left
        the charting gate stuck shut until the player's NEXT move flushed
        it (the 'charting doesn't take until I move 2-3x' bug). The timer
        force-finishes the pending room (area unknown) so the gate always
        advances on its own."""
        self._rating_sent_at = time.time()
        self._sql_rating_pending = True
        self._sql_rating_active = True
        self._sql_rate_overland = False
        if self._sql_rating_job is not None:
            self.root.after_cancel(self._sql_rating_job)
        self._sql_rating_job = self.root.after(
            int(RATING_TIMEOUT * 1000) + 500, self._sql_rating_timeout)
        if self.conn:
            self.conn.send("rating")
            self.last_sent = time.time()

    def _sql_rating_timeout(self):
        """Rating reply never parsed within the window: stop waiting and
        finish the pending room with area unknown, so the gate can't hang."""
        self._sql_rating_job = None
        if not (self._sql_rating_pending or self._sql_rating_active):
            return
        self._sql_rating_pending = False
        self._sql_rating_active = False
        self._sql_chart_dbg("rating timed out - finishing room area-unknown")
        self.sql_finish_room(area="", coder="")

    def sql_rating_capture(self, clean):
        """Swallow a client-initiated rating burst and finalize the
        pending room. Mirrors the TinMap rating_capture mechanics but
        persists into sqlite. Returns True if the line was swallowed."""
        if not (self._sql_rating_pending or self._sql_rating_active):
            return False
        if time.time() - self._rating_sent_at > RATING_TIMEOUT:
            if self._sql_rating_pending:
                self._sql_rating_pending = False
                self.sql_finish_room(area="", coder="")
            self._sql_rating_active = False
            return False
        m = AREA_LINE_RE.match(clean)
        if m:
            area, coder = mapdata.split_area(m.group(1))
            self._sql_rating_pending = False
            self.sql_finish_room(area, coder)
            return True
        if RATING_LINE_RE.match(clean):
            # AREA RATING with no AREA NAME first = overland, area unknown. The
            # overland marker is 'AREA RATING -> Caution is Advised [Mud]' - note
            # it so Explore can tell a real overland border from a capture miss.
            low = clean.lower()
            if "caution is advised" in low or low.rstrip().endswith("[mud]"):
                self._sql_rate_overland = True
            if self._sql_rating_pending:
                self._sql_rating_pending = False
                self.sql_finish_room(area="", coder="")
            return True
        for _p, rx in self.rating_swallow:
            if rx.search(clean):
                # The class-range line is swallowed from the display, but it
                # carries the area's top monster class - grab it for the Agent
                # safety gate before it's eaten (every room-entry rating
                # refreshes it, so the current area's ceiling is always known).
                top = changeling.parse_rating_top(clean)
                if top is not None:
                    self.area_class_top = top
                return True
        return False

    def sql_finish_room(self, area, coder):
        """Persist the pending new room now that area + house status are
        known. [House] -> not charted (submap is out of scope). Plain
        '#map rate' refresh (no pending room) just updates the area."""
        if self._sql_rating_job is not None:   # rating resolved: drop the
            self.root.after_cancel(self._sql_rating_job)  # hard fallback
            self._sql_rating_job = None
        pend = self._sql_pending
        self._sql_pending = None
        is_house = coder.strip().lower() == "house"

        if pend is None:
            v = self._sql_cur_vnum
            if v is not None and area and self.mapdb.has_room(v):
                self.mapdb.set_room_area(
                    v, self.mapdb.get_or_create_area(area, coder))
                self.write_local(f"[map] backfilled area for {v}: {area}",
                                 "#66cc66")
                self.sql_set_status()
            # a re-rate held the charting gate shut (no-BAD timer cancelled on
            # arrival) - reopen it whether or not a name came back, else the
            # walk stalls. Guarded so a manual non-gated #map rate is untouched.
            if self._sql_charting and not self._sql_gate_open:
                self._sql_gate_release()
            return

        obs, prev, via = pend["obs"], pend["from"], pend["via"]
        if is_house:
            self._sql_in_house = True
            self.write_local(
                f"[map] room {obs.vnum} is a player house - left off the "
                "main map (house submap is out of scope)", "#cc9933")
            self.sql_set_status(extra="(in house)")
            self._sql_gate_release()        # room resolved: send next
            return

        self._sql_in_house = False
        area_id = self.mapdb.get_or_create_area(area, coder) if area \
            else None
        self.mapdb.upsert_room(obs.vnum, short_desc=obs.short_desc,
                               area_id=area_id)
        for d in obs.exits:                  # stub the room's own exits
            self.mapdb.ensure_exit(obs.vnum, d)
        # inbound edge, and the reverse edge when the room advertises it
        self._sql_chart(prev, via, obs.vnum)
        rev = REVERSE_DIR.get(via)
        if rev and rev in [e.lower() for e in obs.exits] \
                and prev is not None and self.mapdb.has_room(prev):
            self.mapdb.link_exit(obs.vnum, rev, prev)
        self.write_local(
            f"[mapped {obs.vnum}: {obs.short_desc}"
            + (f" - {area}" if area else " - area unknown") + "]",
            "#66cc66")
        self.sql_set_status()
        self._sql_gate_release()            # new room charted: send next

    def sql_set_status(self, extra=""):
        """Render the neighborhood map pane for the sqlite backend
        (stage 8): a BFS grid of charted rooms around the current one
        (current area only, via MapDB.neighborhood), the current room
        marked '@', and a status line saying whether it is charted, still
        new/mapping, or in a house. Stub exits (known direction, unmapped
        destination) render as dashed spokes so what's unexplored is
        visible. The MAPPING banner doubles as the auto-charting-on
        indicator.
        While Chaossea is active it owns the pane instead (its rooms
        aren't real charted vnums) - this is called unconditionally on
        every DDD/BAD packet regardless of which bot is running, so
        without this guard it stomped _chaossea_render_map's draw on the
        very next packet, every time (confirmed live: the pane never
        showed the path at all)."""
        if self._chaossea_on:
            return
        v = self._sql_cur_vnum
        if v is None or self.mapdb is None:
            self.map.set_graph(None, status="no room yet", mapping=False)
            return
        row = self.mapdb.get_room(v)
        area_name, coder = "", ""
        if row is not None and row["area_id"] is not None:
            a = self.mapdb.conn.execute(
                "SELECT name, author FROM areas WHERE area_id=?",
                (row["area_id"],)).fetchone()
            if a:
                area_name, coder = a["name"], (a["author"] or "")
        # Only the rooms the pane can actually show (~7x7 cells around '@').
        # radius=None scanned the WHOLE area every step - an iter_exits query
        # per room, hundreds of them in a big area like the Ruins of Niroth,
        # all to draw cells that clip off-pane. That O(area) rebuild on every
        # move was the bot's hidden ~1-2s/room stall (and it grew as the bot
        # entered larger areas). A bounded radius makes it O(visible).
        grid, collisions = self.mapdb.neighborhood(
            v, radius=self.setting("map_radius", 5))
        center_area = row["area_id"] if row is not None else None
        payload = {}
        for (x, y), vn in grid.items():
            here = vn == v
            if here and row is None:
                # current room not charted yet: no charted exits exist,
                # so show the live MIP exits as spokes and flag it 'new'.
                exits, stubs = list(self.exits or []), []
            else:
                ex_rows = self.mapdb.iter_exits(vn)
                exits = [e["direction"] for e in ex_rows]
                vn_row = self.mapdb.get_room(vn)
                vn_area = vn_row["area_id"] if vn_row is not None else None
                # A foreign-area border room (placed only for connection
                # context when this pane is centered on the overland - see
                # MapDB.neighborhood) shows its link in, but not its OWN
                # open stubs: those are unexplored ground for ITS area, not
                # this pane's, and rendering them here is what was "bleeding"
                # a dungeon's stubs onto the overland map.
                stubs = ([] if not here and vn_area != center_area else
                         [e["direction"] for e in ex_rows
                          if e["to_vnum"] is None])
            payload[(x, y)] = {
                "rid": vn, "exits": exits, "stubs": stubs,
                "here": here, "collide": (x, y) in collisions,
                "house": here and self._sql_in_house,
                "new": here and row is None,
            }
        if row is not None:
            state = "charted"
        elif self._sql_in_house:
            state = "in house"
        else:
            state = "new/mapping"
        mode = "charting" if self._sql_charting else "following"
        status = f"#{v} {state} - {mode}"
        if extra:
            status += f"  {extra}"
        self.map.set_graph(payload, area=area_name, coder=coder,
                           status=status, mapping=self._sql_charting)

    def sql_map_command(self, a):
        """`#map` subcommands for the sqlite backend. Charting is
        automatic; on/off just pause it. (Pathfinding / landmarks /
        render arrive in later stages.)"""
        if self.mapdb is None:
            self.write_local("[map] sqlite backend not loaded.",
                             "#cc6666")
            return
        if a in ("", "here"):
            v = self._sql_cur_vnum
            if v is None:
                self.write_local("No room yet - walk so MIP reports one.",
                                 "#cc6666")
                return
            row = self.mapdb.get_room(v)
            if row is None:
                self.write_local(f"Room {v}: not charted"
                                 + (" (in house)" if self._sql_in_house
                                    else "") + ".", "#cc9933")
                return
            area = ""
            if row["area_id"] is not None:
                ar = self.mapdb.conn.execute(
                    "SELECT name, author FROM areas WHERE area_id=?",
                    (row["area_id"],)).fetchone()
                if ar:
                    area = ar["name"] + (f" [{ar['author']}]"
                                         if ar["author"] else "")
            self.write_local(
                f"Room {v}: {row['short_desc']}  area: {area or '?'}")
            ex_rows = self.mapdb.iter_exits(v)
            if not ex_rows:
                self.write_local("  exits: (none charted)")
            for e in sorted(ex_rows, key=lambda r: r["direction"]):
                dest = e["to_vnum"]
                notes = []
                if e["command"]:
                    notes.append(f"cmd:{e['command']}")
                if e["setup_command"]:
                    notes.append(f"setup:{e['setup_command']}")
                if e["wait_seconds"]:
                    notes.append(f"wait:{e['wait_seconds']}s")
                note = ("  [" + ", ".join(notes) + "]") if notes else ""
                self.write_local(
                    f"  {e['direction']:<8} -> "
                    f"{('room ' + str(dest)) if dest is not None else '?'}"
                    f"{note}")
        elif a in ("on", "start"):
            self.sql_set_chart_mode(True)
        elif a in ("off", "stop"):
            self.sql_set_chart_mode(False)
        elif a == "rate":
            if self._sql_cur_vnum is None:
                self.write_local("No room yet.", "#cc6666")
                return
            self._sql_pending = None     # refresh area only, don't chart
            self.sql_send_rating()
            self.write_local("[re-rating current room]", "#ffaa44")
        else:
            self.write_local("Usage: #map here | on | off | rate")

    # ---- landmarks + go (stage 5) ----
    # Landmarks are mud-wide (one namespace per map), not per character/guild.
    def sql_landmark(self, arg):
        if self.mapdb is None:
            self.write_local("[map] sqlite backend not loaded.",
                             "#cc6666")
            return
        bits = arg.split()
        if not bits or bits[0] == "list":
            rows = self.mapdb.landmarks_for()
            if not rows:
                self.write_local("No landmarks. "
                                 "#landmark add <tag>.", "#cc9933")
                return
            for r in sorted(rows, key=lambda r: r["tag"]):
                self.write_local(f"  {r['tag']:<16} room {r['vnum']}")
            self.write_local(f"{len(rows)} landmark(s). 'go <tag>' to "
                             "travel.")
            return
        action = bits[0]
        if action not in ("add", "del"):
            self.write_local("Usage: #landmark add|del <tag> "
                             "| #landmark list", "#cc6666")
            return
        if len(bits) < 2:
            self.write_local(f"Usage: #landmark {action} <tag>", "#cc6666")
            return
        tag = bits[1].lower()
        if action == "add":
            v = self._sql_cur_vnum
            if v is None or not self.mapdb.has_room(v):
                self.write_local("Stand in a charted room first.",
                                 "#cc6666")
                return
            self.mapdb.add_landmark(tag, v)
            self.write_local(f"[landmark '{tag}' -> room {v}]", "#66cc66")
        else:
            self.mapdb.remove_landmark(tag)
            self.write_local(f"[landmark '{tag}' removed]", "#66cc66")

    def sql_go(self, tag):
        if self.mapdb is None:
            self.write_local("[map] sqlite backend not loaded.",
                             "#cc6666")
            return
        tag = tag.strip().lower()
        if not tag:
            self.write_local("Usage: #go <landmark>", "#cc6666")
            return
        dst = self.mapdb.resolve_landmark(tag)
        if dst is None:
            self.write_local(f"No landmark '{tag}'. #landmark list to "
                             "see them.", "#cc6666")
            return
        cur = self._sql_cur_vnum
        if cur is None:
            self.write_local("Not located yet - walk so MIP reports a "
                             "room.", "#cc6666")
            return
        if dst == cur:
            self.write_local(f"You're already at '{tag}' (room {dst}).",
                             "#aa88cc")
            return
        edges = self.mapdb.find_path(cur, dst)
        if edges is None:
            self.write_local(f"No path found to '{tag}' (room {dst}).",
                             "#cc6666")
            return
        # speedrun letters are the inverse of the cascade speedwalk map,
        # so the generated string round-trips through expand_speedwalk.
        sw = self.cascade.get("speedwalk", {}) or {}
        rev = {v: k for k, v in sw.items()
               if isinstance(k, str) and len(k) == 1
               and isinstance(v, str)}
        body = mapsql.speedrun_string(edges, rev.get)
        sep = self.setting("command_separator", ";")
        self.write_local(
            f"[Go {tag}: {len(edges)} steps -> {sep}{body}]", "#aa88cc")
        steps, err = self.expand_speedwalk(body)
        if err:
            self.write_local(f"[speedwalk] {err}", "#cc6666")
            return
        for s in steps:
            self.send_line(s, manual=True)
        self._sql_blast_reset()         # Go follows charted edges; don't
        #                                 let its steps mis-chart arrivals

    def _sql_blast_reset(self):
        """Clear the last-move marker after a Go/speedwalk blast. Following
        mode tracks location from DDD (not from sent moves), so there is no
        per-keystroke charting state to unwind here any more; this stays a
        cheap no-op hook for the tin backend's _last_move."""
        self._last_move = None

    # ---- mapfix: special-exit editing (stage 7) ----
    _MAPFIX_FIELDS = {"cmd": "command", "command": "command",
                      "setup": "setup_command", "return": "return_command",
                      "wait": "wait_seconds"}

    @staticmethod
    def _fmt_special_exit(e):
        dest = e["to_vnum"]
        parts = [f"{e['direction']} -> "
                 + (f"room {dest}" if dest is not None else "?")]
        for col, label in (("command", "cmd"), ("setup_command", "setup"),
                           ("return_command", "return")):
            if e[col]:
                parts.append(f"{label}:{e[col]}")
        if e["wait_seconds"]:
            parts.append(f"wait:{e['wait_seconds']}s")
        return "  ".join(parts)

    def sql_mapfix(self, arg):
        if self.mapdb is None:
            self.write_local("[map] sqlite backend not loaded.",
                             "#cc6666")
            return
        v = self._sql_cur_vnum
        bits = arg.split(None, 2)
        if not bits:                                   # list specials here
            if v is None:
                self.write_local("No room yet.", "#cc6666")
                return
            specials = [e for e in self.mapdb.iter_exits(v)
                        if e["command"] or e["setup_command"]
                        or e["return_command"] or e["wait_seconds"]]
            if not specials:
                self.write_local(
                    "No special exits here. Usage: #mapfix <dir> "
                    "cmd|setup|return|wait <value> | relink <vnum|clear> "
                    "| <dir> clear",
                    "#cc9933")
                return
            for e in specials:
                self.write_local("  " + self._fmt_special_exit(e))
            return
        if v is None or not self.mapdb.has_room(v):
            self.write_local("Stand in a charted room first.", "#cc6666")
            return
        direction = bits[0].lower()
        if len(bits) == 1:                             # show one exit
            e = self.mapdb.get_exit(v, direction)
            if e is None:
                self.write_local(f"No exit '{direction}' here "
                                 "(set one with #mapfix <dir> cmd ...).",
                                 "#cc9933")
            else:
                self.write_local("  " + self._fmt_special_exit(e))
            return
        sub = bits[1].lower()
        if sub == "clear":
            for col in ("command", "setup_command", "return_command"):
                self.mapdb.set_exit_field(v, direction, col, None)
            self.mapdb.set_exit_field(v, direction, "wait_seconds", 0)
            self.write_local(f"[mapfix {direction}: special fields "
                             "cleared]", "#66cc66")
            return
        if sub == "relink":
            # fix/clear a wrong destination (to_vnum). 'clear' NULLs it so
            # the edge re-charts on the next traversal; a vnum points it at
            # a charted room. Command/setup/return columns are untouched.
            target = bits[2].strip().lower() if len(bits) > 2 else ""
            if target in ("", "clear", "none", "null"):
                self.mapdb.link_exit(v, direction, None)
                self.write_local(
                    f"[mapfix {direction}: destination cleared - will "
                    "re-chart on next traversal]", "#66cc66")
                return
            try:
                tv = int(target)
            except ValueError:
                self.write_local("relink needs a room vnum or 'clear'.",
                                 "#cc6666")
                return
            if not self.mapdb.has_room(tv):
                self.write_local(f"Room {tv} isn't charted - can't link "
                                 "to it.", "#cc6666")
                return
            self.mapdb.link_exit(v, direction, tv)
            self.write_local("[mapfix] " + self._fmt_special_exit(
                self.mapdb.get_exit(v, direction)), "#66cc66")
            return
        field = self._MAPFIX_FIELDS.get(sub)
        if field is None:
            self.write_local("Usage: #mapfix <dir> "
                             "cmd|setup|return|wait <value> | relink "
                             "<vnum|clear> | clear | <dir>", "#cc6666")
            return
        value = bits[2] if len(bits) > 2 else ""
        if field == "wait_seconds":
            try:
                value = int(value)
            except ValueError:
                self.write_local("wait needs an integer (seconds).",
                                 "#cc6666")
                return
        else:
            value = value or None          # empty value clears that field
        self.mapdb.set_exit_field(v, direction, field, value)
        self.write_local("[mapfix] " + self._fmt_special_exit(
            self.mapdb.get_exit(v, direction)), "#66cc66")

    # ---- Toggle command -------------------------------------------------
    # Alternate spellings accepted for a toggle name.
    _TOGGLE_ALIASES = {"mapdetails": "mapdetail"}

    def _toggle_registry(self):
        """name -> (short description, current on/off, flip callable). The
        home for side-pane switches; future ones (dmg/rnd, xp deltas, ...)
        slot in here."""
        reg = {
            "charting": ("map building", self._sql_charting,
                         self.sql_chart_toggle),
            "mapdetail": ("room info in pane", self.show_mapdetail,
                          self._toggle_mapdetail),
            "flat": ("Hunt skips up/down exits", self.hunt_flat,
                     self._toggle_hunt_flat),
        }
        if self.guild.lower() == "necromancers":
            reg["necroguard"] = ("auto con/protection/veil",
                                 self.necro_guard, self._toggle_necroguard)
        if self.guild.lower() == "bladesingers":
            reg["blur"] = ("auto-blur before reset wastes charges",
                           self.blur_auto, self._toggle_blurauto)
        for name in sorted(set(self.trigger_toggle_of.values())):
            pats = [p for p, n in self.trigger_toggle_of.items()
                    if n == name]
            desc = "trigger: " + ", ".join(pats)
            reg[name] = (desc[:40],
                         self.trigger_toggles.get(name, True),
                         (lambda n=name: self._toggle_trigger(n)))
        # The corpse routine's gate (e.g. 'harvest') is a Toggle switch too,
        # registered here so it stays listable/flippable now that it's no
        # longer carried by a trigger's "toggle" key.
        ctog = (self.corpse_cfg or {}).get("toggle")
        if ctog and ctog not in reg:
            reg[ctog] = ("corpse loot routine",
                         self.trigger_toggles.get(ctog, True),
                         (lambda n=ctog: self._toggle_trigger(n)))
        return reg

    def cmd_toggle(self, arg):
        """`Toggle <name>` flips a switch; `Toggle` / `Toggles` (or no
        name) lists them all with their on/off state."""
        reg = self._toggle_registry()
        name = arg.strip().lower()
        name = self._TOGGLE_ALIASES.get(name, name)
        if not name:
            self.write_local("Toggles:")
            for key, (desc, state, _flip) in reg.items():
                self.write_local(
                    f"  {key:<11} {('on' if state else 'off'):<3}  {desc}")
            return
        if name not in reg:
            self.write_local(f"No toggle '{name}'. 'Toggle' lists them.",
                             "#cc9933")
            return
        reg[name][2]()

    def _toggle_mapdetail(self):
        self.show_mapdetail = not self.show_mapdetail
        self.set_setting("show_mapdetail", self.show_mapdetail)
        self.write_local(
            f"[mapdetail {'on' if self.show_mapdetail else 'off'}]",
            "#44aaff")
        self.update_info()

    def _toggle_hunt_flat(self):
        self.hunt_flat = not self.hunt_flat
        self.set_setting("hunt_flat", self.hunt_flat)
        self.write_local(
            f"[flat {'on' if self.hunt_flat else 'off'}] Hunt "
            f"{'skips' if self.hunt_flat else 'allows'} up/down exits.",
            "#44aaff")

    def _toggle_necroguard(self):
        self.necro_guard = not self.necro_guard
        self.set_setting("necro_guard", self.necro_guard)
        self.write_local(
            f"[necroguard {'on' if self.necro_guard else 'off'}"
            " - auto con/protection/veil]", "#44aaff")

    def _toggle_blurauto(self):
        self.blur_auto = not self.blur_auto
        self.set_setting("blur_auto", self.blur_auto)
        self.write_local(
            f"[blur {'on' if self.blur_auto else 'off'}"
            " - auto-fire the kickoff blur before a reset wastes charges]",
            "#44aaff")

    def _toggle_trigger(self, name):
        new = not self.trigger_toggles.get(name, True)
        self.trigger_toggles[name] = new
        self.set_setting("trigger_toggles", self.trigger_toggles)
        self.write_local(
            f"[{name} {'on' if new else 'off'}"
            f" - {'' if new else 'not '}firing its trigger(s)]", "#44aaff")

    # ---- chart mode + room editing commands (Chart/Maproom/...) ----
    def sql_chart_toggle(self):
        """`Chart` - toggle map-building mode (see sql_set_chart_mode)."""
        if self.mapdb is None:
            self.write_local("[map] sqlite backend not loaded.", "#cc6666")
            return
        # A manual toggle is an explicit override: cancel any pending
        # combat auto-resume so the user's choice sticks through the fight.
        self._sql_chart_combat_paused = False
        if self._sql_chart_resume_job is not None:
            self.root.after_cancel(self._sql_chart_resume_job)
            self._sql_chart_resume_job = None
        self.sql_set_chart_mode(not self._sql_charting)

    # in_combat is a property so EVERY setter (FFF round/enemy, damage tracker,
    # kill trigger, bot/run logic) funnels combat-edge detection through one
    # place - used to auto-pause manual charting during a fight.
    @property
    def in_combat(self):
        return self._in_combat

    @in_combat.setter
    def in_combat(self, value):
        value = bool(value)
        prev = getattr(self, "_in_combat", False)
        self._in_combat = value
        if value != prev:
            self._on_combat_change(value)

    def _on_combat_change(self, fighting):
        """Combat just started or ended. Auto-pause MANUAL charting for the
        fight so typed commands (attack/flee/heal) flow immediately instead
        of queuing behind the lockstep gate, then resume when it's over.

        Skipped while an auto-walker (Bot/Hunt/Run/Explore/Agent) drives -
        those manage the gate and combat themselves. Resume is debounced so
        a flapping enemy field or an immediate next mob doesn't thrash the
        mode (and post-kill loot/text settles first)."""
        if self.map_backend != "sqlite" or self.mapdb is None:
            return
        if self._bot_on or self._run_on or self._explore_on or self._agent_on:
            return
        if fighting:
            if self._sql_chart_resume_job is not None:
                self.root.after_cancel(self._sql_chart_resume_job)
                self._sql_chart_resume_job = None
            if self._sql_charting and not self._sql_chart_combat_paused:
                self._sql_chart_combat_paused = True
                self.write_local("[chart auto-paused - combat; resumes after "
                                 "the fight]", "#ffaa44")
                self.sql_set_chart_mode(False, announce=False)
        elif self._sql_chart_combat_paused:
            if self._sql_chart_resume_job is not None:
                self.root.after_cancel(self._sql_chart_resume_job)
            self._sql_chart_resume_job = self.root.after(
                1500, self._sql_chart_resume)

    def _sql_chart_resume(self):
        """Debounced resume of charting paused for combat."""
        self._sql_chart_resume_job = None
        if self.in_combat or not self._sql_chart_combat_paused:
            return                          # re-engaged / overridden meanwhile
        self._sql_chart_combat_paused = False
        if not self._sql_charting:
            self.write_local("[chart resumed]", "#ffaa44")
            self.sql_set_chart_mode(True, announce=False)

    def sql_maproom(self):
        """`Maproom` - details of the current room: area, coder, room id,
        charted exits, and the player's notes."""
        if self.mapdb is None:
            self.write_local("[map] sqlite backend not loaded.", "#cc6666")
            return
        v = self._sql_cur_vnum
        if v is None:
            self.write_local("No room yet - walk so a room packet "
                             "reports one.", "#cc6666")
            return
        row = self.mapdb.get_room(v)
        if row is None:
            self.write_local(
                f"Room {v}: not charted"
                + (" (in house)" if self._sql_in_house else "")
                + ". Use Chart to add it.", "#cc9933")
            return
        area, coder = "?", ""
        if row["area_id"] is not None:
            ar = self.mapdb.conn.execute(
                "SELECT name, author FROM areas WHERE area_id=?",
                (row["area_id"],)).fetchone()
            if ar:
                area = ar["name"] or "?"
                coder = ar["author"] or ""
        self.write_local(f"Room {v}: {row['short_desc']}", "#ccccff")
        self.write_local(f"  area: {area}"
                         + (f"   coder: {coder}" if coder else ""))
        ex_rows = self.mapdb.iter_exits(v)
        if not ex_rows:
            self.write_local("  exits: (none charted)")
        for e in sorted(ex_rows, key=lambda r: r["direction"]):
            dest = e["to_vnum"]
            notes = []
            if e["command"]:
                notes.append(f"cmd:{e['command']}")
            if e["setup_command"]:
                notes.append(f"setup:{e['setup_command']}")
            if e["return_command"]:
                notes.append(f"return:{e['return_command']}")
            if e["wait_seconds"]:
                notes.append(f"wait:{e['wait_seconds']}s")
            note = ("  [" + ", ".join(notes) + "]") if notes else ""
            self.write_local(
                f"  {e['direction']:<8} -> "
                f"{('room ' + str(dest)) if dest is not None else '?'}"
                f"{note}")
        if row["notes"]:
            self.write_local("  notes:", "#ccaa66")
            for ln in row["notes"].splitlines():
                self.write_local(f"    {ln}", "#ccaa66")

    def sql_maprerate(self):
        """`Maprerate` - force a fresh `rating` for the CURRENT room and write
        its area, in any mode. For backfilling a room whose area came up NULL
        (a charting-time capture miss). Watch for '[map] backfilled area...'."""
        if self.map_backend != "sqlite" or self.mapdb is None:
            self.write_local("[map] needs the sqlite backend.", "#cc6666")
            return
        v = self._sql_cur_vnum
        if v is None:
            self.write_local("No room yet.", "#cc6666")
            return
        if not self.mapdb.has_room(v):
            self.write_local(f"[map] room {v} isn't charted - Chart it first.",
                             "#cc9933")
            return
        cur = self.mapdb.get_room(v)
        had = cur["area_id"]
        self.write_local(
            f"[map] re-rating room {v} (area_id now: {had})...", "#88aacc")
        self.sql_send_rating()

    def sql_mapnote(self, arg):
        """`Mapnote <text>` appends a note line to the current room;
        `Mapnote` alone shows it, `Mapnote clear` wipes it."""
        if self.mapdb is None:
            self.write_local("[map] sqlite backend not loaded.", "#cc6666")
            return
        v = self._sql_cur_vnum
        if v is None or not self.mapdb.has_room(v):
            self.write_local("Stand in a charted room first.", "#cc6666")
            return
        text = arg.strip()
        cur = self.mapdb.get_room(v)["notes"] or ""
        if not text:
            self.write_local(f"Room {v} notes: {cur or '(none)'}")
            return
        if text.lower() == "clear":
            self.mapdb.set_room_note(v, None)
            self.write_local(f"[room {v}: notes cleared]", "#66cc66")
            return
        new = (cur + "\n" + text) if cur else text
        self.mapdb.set_room_note(v, new)
        self.write_local(f"[room {v}: note added]", "#66cc66")

    def sql_maplink(self, arg):
        """`Maplink <direction> [command...] <destvnum>` manually
        (re)points an exit. <direction> is the canonical token; the middle
        words, if any, are the literal command to send when it differs
        from the token; <destvnum> is the room it leads to (stubbed if not
        yet charted). The exit's setup/return/wait columns are preserved."""
        if self.mapdb is None:
            self.write_local("[map] sqlite backend not loaded.", "#cc6666")
            return
        v = self._sql_cur_vnum
        if v is None or not self.mapdb.has_room(v):
            self.write_local("Stand in a charted room first.", "#cc6666")
            return
        bits = arg.split()
        if len(bits) < 2:
            self.write_local("Usage: Maplink <direction> [command] "
                             "<destvnum>", "#cc6666")
            return
        direction = bits[0].lower()
        try:
            dest = int(bits[-1])
        except ValueError:
            self.write_local("Last argument must be the destination room "
                             "vnum.", "#cc6666")
            return
        command = " ".join(bits[1:-1]) or None
        if not self.mapdb.has_room(dest):
            self.mapdb.upsert_room(dest)        # stub so the link's FK holds
            self.write_local(f"[room {dest} stubbed - walk there to fill "
                             "in its details]", "#cc9933")
        self.mapdb.link_exit(v, direction, dest)
        if command and command.lower() != direction:
            self.mapdb.set_exit_field(v, direction, "command", command)
        else:
            self.mapdb.set_exit_field(v, direction, "command", None)
        self.write_local("[maplink] " + self._fmt_special_exit(
            self.mapdb.get_exit(v, direction)), "#66cc66")
        self.sql_set_status()

    def sql_mapunlink(self, arg):
        """`Mapunlink <direction>` or `Mapunlink <command>` removes an
        exit from the current room (a phantom or a wrong link)."""
        if self.mapdb is None:
            self.write_local("[map] sqlite backend not loaded.", "#cc6666")
            return
        v = self._sql_cur_vnum
        if v is None or not self.mapdb.has_room(v):
            self.write_local("Stand in a charted room first.", "#cc6666")
            return
        key = arg.strip()
        if not key:
            self.write_local("Usage: Mapunlink <direction> | <command>",
                             "#cc6666")
            return
        kl = key.lower()
        if kl in self.mapdb.exit_dirs(v):
            self.mapdb.delete_exit(v, kl)
            self.write_local(f"[unlinked {kl} from room {v}]", "#66cc66")
            self.sql_set_status()
            return
        for e in self.mapdb.iter_exits(v):
            if (e["command"] or "").lower() == kl:
                self.mapdb.delete_exit(v, e["direction"])
                self.write_local(
                    f"[unlinked {e['direction']} ({key}) from room {v}]",
                    "#66cc66")
                self.sql_set_status()
                return
        self.write_local(f"No exit '{key}' here.", "#cc9933")

    def _sql_move_plan(self, direction):
        """Resolve a single-word movement against the current room's
        charted exit. Returns (seq, to_vnum, wait):
          seq     - command sequence to send instead of the bare direction
                    when the exit carries a special setup/command (mapfix's
                    'numpad-7 for nw sends climb over logs'); None means
                    'send the typed direction unchanged'.
          to_vnum - the exit's charted destination (or None).
          wait    - the exit's wait_seconds (0 if none). On a MANUAL move
                    this paces the step (send setup, wait, then move); Go
                    avoids wait>0 exits entirely (find_path excludes them)."""
        if self.mapdb is None or self._sql_cur_vnum is None:
            return None, None, 0
        e = self.mapdb.get_exit(self._sql_cur_vnum, direction)
        if e is None:
            return None, None, 0
        setup, cmd = e["setup_command"], e["command"]
        seq = None
        if setup or (cmd and cmd != direction):
            seq = []
            if setup:
                seq.append(setup)
            seq.append(cmd if cmd else direction)
        return seq, e["to_vnum"], e["wait_seconds"]

    @property
    def LANDMARK_FILE(self):
        return os.path.join(paths.mud_dir(self.mud), "landmarks.json")

    def load_landmarks(self):
        data, _err = paths.load_json(self.LANDMARK_FILE, default={})
        self.landmarks = data

    def save_landmarks(self):
        paths.save_json(self.LANDMARK_FILE, self.landmarks)

    def map_locate(self, vnum=None):
        if not self.locator or not self.room:
            return
        was_located = self.locator.located
        prev_rid = self.locator.room_id
        rid = self.locator.on_room(self.room, self.exits,
                                   last_cmd=self._last_move,
                                   vnum=vnum)
        if rid is None:
            self.maybe_map_new_room(prev_rid, was_located)
            rid = self.locator.room_id
        if rid is not None:
            if self.mapping and rid not in self._session_new_rooms \
                    and rid < mapdata.PATCH_RID_BASE:
                self.exit_mapping("tracking returned to known "
                                  "territory")
            self.handle_record(prev_rid, rid)
            room = self.tmap.rooms[rid]
            grid, collisions = self.tmap.neighborhood(rid, 3)
            payload = {}
            for (x, y), gid in grid.items():
                r = self.tmap.rooms.get(gid)
                if r is None:
                    continue
                _nm, exits, _v = mapdata.split_name(r.name)
                payload[(x, y)] = {"rid": gid, "exits": exits,
                                   "here": gid == rid,
                                   "collide": (x, y) in collisions,
                                   "house": r.house}
            self.map.set_graph(payload, area=room.area,
                               coder=room.coder,
                               status=f"#{rid}", mapping=self.mapping)
        else:
            n = len(self.locator.cands)
            self.map.set_graph(None,
                               status=(f"locating... ({n} candidates)"
                                       if n else "not on map"),
                               mapping=self.mapping)

    # -------------------------------------------- mapping mode (6.3)
    def enter_mapping(self, auto=False):
        if self.mapping:
            return
        self.mapping = True
        self.mapping_auto = auto
        n = 0
        if self.locator and self.locator.located and self.tmap:
            cur = self.tmap.rooms.get(self.locator.room_id)
            if cur and cur.area:
                n = sum(1 for r in self.tmap.rooms.values()
                        if r.area == cur.area)
        self.write_local(
            f"[mapping mode{' (auto)' if auto else ''}: new rooms "
            f"will be rated and added automatically; {n} rooms in "
            "current area]", "#ffaa44")

    def exit_mapping(self, why=""):
        if not self.mapping:
            return
        self.mapping = False
        self.write_local(
            f"[mapping mode off{': ' + why if why else ''}]",
            "#ffaa44")

    def maybe_map_new_room(self, prev_rid, was_located):
        """Locator lost us. Auto-enter mapping ONLY on a confident fix
        for the previous room (spec 6.3 safeguard: a lost locator in
        mapped territory must not trigger spurious mapping); in
        mapping mode, create the room from observed MIP data."""
        if not self.tmap:
            return
        if not self.mapping:
            if was_located and prev_rid is not None and \
                    self._last_move and \
                    self.setting("auto_mapping", True):
                self.enter_mapping(auto=True)
            else:
                return
        if not self.mapping or not self._last_move or prev_rid is None:
            return
        name, exits, _v = mapdata.split_name(self.room)
        if not name:
            return
        use_exits = self.exits or exits
        full = f"{name} ({','.join(use_exits)})" if use_exits else name
        rid = self.tmap.alloc_rid()
        room_patch = {"op": "add_room", "room": rid, "name": full}
        edge_in = {"op": "add_edge", "room": prev_rid,
                   "exit": self._last_move, "to": rid}
        self.tmap.apply_patch(room_patch, "live")
        self.tmap.apply_patch(edge_in, "live")
        patches = [room_patch, edge_in]
        lm = self._last_move.lower()
        if lm in REVERSE_DIR and REVERSE_DIR[lm] in \
                [e.lower() for e in use_exits]:
            edge_back = {"op": "add_edge", "room": rid,
                         "exit": REVERSE_DIR[lm], "to": prev_rid}
            self.tmap.apply_patch(edge_back, "live")
            patches.append(edge_back)
        self._session_new_rooms.add(rid)
        self.locator.force(rid)
        self._pending_persist = (rid, patches)
        self.send_rating(rid)

    def send_rating(self, rid):
        """Mapping mode only: auto-send rating, swallow the response
        (spec 6.5)."""
        self._rating_pending_rid = rid
        self._rating_sent_at = time.time()
        self._rating_got_name = False
        if self.conn:
            self.conn.send("rating")
            self.last_sent = time.time()

    def rating_capture(self, clean):
        """True = line swallowed (client-initiated rating response).

        The window stays open for the WHOLE burst (the AREA RATING
        and Monster-class lines arrive AFTER the name line), and an
        AREA RATING line with no preceding AREA NAME means an
        overland room - finalize immediately as area-unknown rather
        than waiting out the timeout. Conservative: only known
        patterns are eaten; anything else flows through."""
        if self._rating_pending_rid is None and \
                not getattr(self, "_rating_got_name", False):
            return False
        if time.time() - self._rating_sent_at > RATING_TIMEOUT:
            if self._rating_pending_rid is not None:
                self._rating_pending_rid = None
                self.finish_pending_room(area="", coder="")
            self._rating_got_name = False
            return False
        m = AREA_LINE_RE.match(clean)
        if m:
            area, coder = mapdata.split_area(m.group(1))
            rid = self._rating_pending_rid
            self._rating_pending_rid = None
            self._rating_got_name = True
            room = self.tmap.rooms.get(rid) if (self.tmap and
                                                rid is not None) \
                else None
            if room:
                room.area, room.coder = area, coder
            self.finish_pending_room(area=area, coder=coder)
            return True
        if RATING_LINE_RE.match(clean):
            if self._rating_pending_rid is not None:
                # rating line arrived with NO name line first:
                # overland room, no area to record
                self._rating_pending_rid = None
                self._rating_got_name = True
                self.finish_pending_room(area="", coder="")
            return True
        for _p, rx in self.rating_swallow:
            if rx.search(clean):
                return True
        return False

    def finish_pending_room(self, area, coder):
        """Persist held patches now that area (and House status) is
        known. [House] -> character scope (spec 6.2.1); else -> mud
        map-extra sidecar."""
        pending = self._pending_persist
        if not pending:
            return
        rid, patches = pending
        self._pending_persist = None
        is_house = coder.lower() == "house"
        for p in patches:
            if p["op"] == "add_room":
                p["area"] = area
                p["coder"] = coder
                if is_house:
                    p["house"] = True
        room = self.tmap.rooms.get(rid) if self.tmap else None
        if room:
            room.house = is_house
        if is_house:
            err = None
            for p in patches:
                err = err or self.cascade.save_entry(
                    "character", "map_patches", None, p)
            scope_txt = "character (house)"
        else:
            err = mapdata.append_extra_patches(self.mud, patches)
            scope_txt = "mud"
        tag = room.name if room else str(rid)
        if err:
            self.write_local(f"[map] persist failed: {err}", "#cc6666")
        else:
            self.write_local(
                f"[mapped: {tag}"
                + (f" - {area}" if area else " - area unknown")
                + f" -> {scope_txt} scope]", "#66cc66")
        self.map_locate()

    # ------------------------------------------ record override (6.3)
    def arm_record(self, scope):
        if self.map_backend == "sqlite":
            self.write_local("[#record is tin-only; use #mapfix (or "
                             "Mapfix) for sqlite special exits.]",
                             "#cc9933")
            return
        if scope not in config.LAYER_SCOPES:
            self.write_local(
                "Scope must be character|guild|mud|global", "#cc6666")
            return
        if scope == "guild" and self.guild.lower() == "none":
            self.write_local("No guild on this profile - guild scope "
                             "unavailable.", "#cc6666")
            return
        if not (self.locator and self.locator.located):
            self.write_local("Not located - stand somewhere known "
                             "first.", "#cc6666")
            return
        self._record_scope = scope
        self.write_local(
            f"[record armed -> {self.cascade.label_for(scope)}: issue "
            "the command, arrive, and the observed edge is saved]",
            "#ffaa44")

    def handle_record(self, prev_rid, new_rid):
        if not self._record_scope or prev_rid is None or \
                not self._last_move or prev_rid == new_rid:
            return
        scope = self._record_scope
        self._record_scope = None
        prev_room = self.tmap.rooms.get(prev_rid)
        op = "redirect" if (prev_room and self._last_move in
                            prev_room.exits) else "add_edge"
        patch = {"op": op, "room": prev_rid,
                 "exit": self._last_move, "to": new_rid}
        self.tmap.apply_patch(patch, scope)
        err = self.cascade.save_entry(scope, "map_patches", None,
                                      patch)
        if err:
            self.write_local(f"[record] save failed: {err}",
                             "#cc6666")
        else:
            self.write_local(
                f"[recorded: room {prev_rid} '{self._last_move}' -> "
                f"room {new_rid} at {self.cascade.label_for(scope)}]",
                "#66cc66")

    # ------------------------------------------------ go / walking
    def resolve_targets(self, name):
        name = name.strip().lower()
        if name in self.landmarks:
            return [(self.landmarks[name]["rid"],
                     self.landmarks[name].get("desc", ""))]
        return [(rid, desc)
                for rid, _typ, desc in self.speedruns.get(name, [])]

    def resolve_landmark(self, name):
        t = self.resolve_targets(name)
        return t[0] if t else (None, None)

    def start_go(self, name):
        if self.map_backend == "sqlite":
            self.sql_go(name)
            return
        if not self.tmap:
            self.write_local("Map not loaded.", "#cc6666")
            return
        targets = self.resolve_targets(name)
        if not targets:
            self.write_local(f"No landmark or speedrun '{name}'. "
                             "#speedruns to list.", "#cc6666")
            return
        if not self.locator or not self.locator.located:
            self.write_local("Not located on the map yet - walk a "
                             "room or two so I can find you.",
                             "#cc6666")
            return
        path, dst, used_avoid = self.tmap.path_any_nearest(
            self.locator.room_id, [r for r, _d in targets])
        if path is None:
            self.write_local(f"No path to '{name}' from here.",
                             "#cc6666")
            return
        if not path:
            self.write_local("You're already there.", "#aa88cc")
            return
        self.cancel_walk(quiet=True)
        self.walk = list(path)
        desc = dict(targets).get(dst, "")
        note = (f" - nearest of {len(targets)} '{name}' entries"
                if len(targets) > 1 else "")
        note += " - via avoid-flagged exits" if used_avoid else ""
        self.write_local(f"[go {name}: {len(path)} steps{note}"
                         + (f" - {desc}" if desc else "") + "]",
                         "#aa88cc")
        self._walk_step()

    def walk_to_room(self, rid):
        if not (self.tmap and self.locator and self.locator.located):
            return
        path, _used_avoid = self.tmap.path_any(self.locator.room_id,
                                               rid)
        if not path:
            return
        self.cancel_walk(quiet=True)
        self.walk = list(path)
        self._walk_step()

    def cancel_walk(self, quiet=False):
        if self._walk_job:
            self.root.after_cancel(self._walk_job)
            self._walk_job = None
        if self.walk and not quiet:
            self.write_local(f"[walk cancelled, {len(self.walk)} "
                             "steps remaining]", "#aa88cc")
        self.walk = []
        # A cancelled/aborted walk never reached its goal, so any pending
        # arrival callback (e.g. the next leg of a VN errand) is dropped.
        self._walk_done_cb = None

    def _fire_walk_done(self):
        """A walk reached its goal: run the one-shot arrival callback, if
        any. Cleared first so a callback that starts a new walk can set its
        own without it being wiped here."""
        cb = self._walk_done_cb
        self._walk_done_cb = None
        if cb:
            cb()

    def _walk_step(self):
        self._walk_job = None
        if not self.walk:
            return
        if self.in_combat:
            self.write_local("[walk aborted: combat]", "#ff5555")
            self.walk = []
            self._walk_done_cb = None
            return
        if self.deadman_tripped:
            self.write_local("[walk aborted: deadman]", "#ff5555")
            self.walk = []
            self._walk_done_cb = None
            return
        ms = self.setting("walk_ms", 0)
        if ms <= 0:
            steps = self.walk
            self.walk = []
            for cmd in steps:
                self.send_line(cmd)
            self.write_local(f"[sent {len(steps)} steps]", "#aa88cc")
            self._fire_walk_done()
            return
        cmd = self.walk.pop(0)
        self.send_line(cmd)
        if self.walk:
            self._walk_job = self.root.after(ms, self._walk_step)
        else:
            self.write_local("[arrived]", "#aa88cc")
            self._fire_walk_done()

    # ------------------------------------------------ area roaming bot
    # A deliberately basic hunter: roam every charted room sharing the
    # CURRENT room's area (no path, no per-area setup), pausing whenever a
    # mob engages so the MUD's autocombat + the kill trigger handle the
    # fight, then resuming. Stop with `Bot off` / `#stop` / disconnect.
    # ---- room presence markers --------------------------------------------
    # The MUD asets `look_monster italics` (SGR 3) and `look_player underline`
    # (SGR 4) wrap mob / player lines in the auto-look that lands when you
    # enter a room. The AnsiParser carries those as the "I" / "U" tag bits;
    # italics has no font so it's invisible, underline shows (fine). This is
    # the instant presence signal the bot reads instead of waiting + parsing.
    @staticmethod
    def _line_markers(spans):
        """(has_italic, has_underline) for one line's spans - True if ANY
        non-blank span carries that marker."""
        ital = under = False
        for text, tag in spans:
            if not tag or not text.strip():
                continue
            parts = tag.split("_")
            if "I" in parts:
                ital = True
            if "U" in parts:
                under = True
        return ital, under

    def _report_line_markers(self, spans, clean):
        ital, under = self._line_markers(spans)
        if ital and under:
            self.write_local(f"[marker] mob+player: {clean}", "#cc66cc")
        elif ital:
            self.write_local(f"[marker] mob: {clean}", "#cc66cc")
        elif under:
            self.write_local(f"[marker] player: {clean}", "#cc66cc")

    def cmd_bot(self, arg):
        self._bot_command(arg, "aggro")

    def cmd_hunt(self, arg):
        self._bot_command(arg, "hunt")

    def _bot_command(self, arg, mode):
        verb = "Bot" if mode == "aggro" else "Hunt"
        a = arg.strip().lower()
        if a in ("off", "stop", "halt"):
            if self._bot_on:
                self._bot_stop("stopped")
            else:
                self.write_local(f"{verb} is not running.", "#cc9933")
            return
        if a == "debug":
            self._bot_debug = not self._bot_debug
            self.write_local(
                f"[bot] debug {'on' if self._bot_debug else 'off'} - "
                "reports per-room: ready time + trigger + mob/player flags + "
                "decision.", "#6699cc")
            return
        if self._bot_on:
            running = "Bot" if self._bot_mode == "aggro" else "Hunt"
            self.write_local(
                f"{running} already roaming"
                + (f" '{self._bot_area_name}'" if not self._bot_mapless
                   else " (mapless)")
                + f". {running} off to stop.", "#cc9933")
            return
        if mode == "hunt" and a in self._REVERSE_DIR:
            fence_vnum = self._sql_cur_vnum
            if fence_vnum is None:
                self.write_local("[hunt] location unknown.", "#cc9933"); return
            self.send_line(a)
            self._bot_start(mode, fence_vnum=fence_vnum,
                            fence_dir=self._REVERSE_DIR[a])
            return
        self._bot_start(mode, mapless=(a == "mapless"))

    # ============================================================ Agent
    # Autonomous assistant (agent_spec.md). SCAFFOLD STAGE: the tick loop
    # runs and reports the goal it WOULD pursue each tick; it does not yet
    # move/fight/heal. Behaviour is added guild-by-guild (Changeling first),
    # reusing the _bot_* marker/assess/home helpers rather than forking them.
    def cmd_agent(self, arg):
        a = arg.strip().lower()
        if a in ("off", "stop", "halt"):
            if self._agent_on:
                self._agent_stop("stopped")
            else:
                self.write_local("Agent is not running.", "#cc9933")
            return
        if a == "debug":
            self._agent_debug = not self._agent_debug
            self.write_local(
                f"[agent] debug {'on' if self._agent_debug else 'off'} - "
                "reports the goal chosen each tick.", "#6699cc")
            return
        if a in ("", "status"):
            self._agent_status()
            return
        if a in ("on", "full", "hunt", "explore"):
            if self._agent_on:
                self.write_local(
                    f"Agent already running in '{self._agent_area_name}'. "
                    "Agent off to stop.", "#cc9933")
                return
            mode = "full" if a in ("on", "") else a
            self._agent_start(mode)
            return
        self.write_local(
            "Usage: Agent [on|hunt|explore|off|debug|status]", "#cc9933")

    def _agent_status(self):
        if not self._agent_on:
            self.write_local("[agent] off.", "#aa88cc")
            return
        self.write_local(
            f"[agent] mode={self._agent_mode} phase={self._agent_phase} "
            f"goal={self._agent_goal or '?'} area='{self._agent_area_name}' "
            f"hp={self._agent_hp_pct()}% pools={self._agent_pool_pct()}% "
            f"(area top={self.area_class_top}, best kill={self.changeling_best_kill})",
            "#6699cc")

    def _agent_s(self, key, default_ms):
        return max(0.1, float(self.setting(key, default_ms)) / 1000.0)

    def _agent_hp_pct(self):
        cur = self.vitals.get("hp")
        top = self.vitals.get("hpmax") or self.vitals_max.get("hp", 0)
        if not cur or not top:
            return 100
        return int(100 * cur / top)

    def _agent_pool_pct(self):
        """Lowest of the guild pools (gp1/gp2) as a %, or 100 if none. For
        changelings gp1=Protoplasm, gp2=Stamina (both already 0-100)."""
        best = 100
        for f in ("gp1", "gp2"):
            cur = self.vitals.get(f)
            top = self.vitals.get(f + "max") or self.vitals_max.get(f, 0)
            if cur is not None and top:
                best = min(best, int(100 * cur / top))
        return best

    def _agent_start(self, mode="full"):
        if self.map_backend != "sqlite" or self.mapdb is None:
            self.write_local("[agent] needs the sqlite map backend.", "#cc6666")
            return
        v = self._sql_cur_vnum
        if v is None:
            self.write_local("[agent] location unknown - move a room so the "
                             "map locates you, then Agent on.", "#cc6666")
            return
        area_id = self._bot_area_of(v)
        if area_id is None:
            self.write_local("[agent] this room has no charted area - can't "
                             "scope the run. Map it first.", "#cc6666")
            return
        self._agent_on = True
        self._agent_mode = mode
        # Phased plan (user decision): chart the whole area first, then sweep.
        # An explore-only run never reaches sweep; a hunt-only run skips chart.
        self._agent_phase = "sweep" if mode == "hunt" else "chart"
        self._agent_area_id = area_id
        self._agent_area_name = self._area_name(area_id)
        self._agent_start_vnum = v
        self._agent_goal = ""
        self._agent_homing = False
        self._agent_explore_started = False
        self._agent_explore_retries = 0
        self._agent_hunt_started = False
        self._agent_recharting = False
        self._agent_prechart_rooms = 0
        self._agent_gate_until = 0.0
        self._agent_heal_at = self._agent_flee_at = self._agent_form_at = 0.0
        self.write_local(
            f"[agent] running in '{self._agent_area_name}' (mode={mode}). "
            "Phased: chart the area, then sweep it - healing/fleeing via the "
            f"{self.guild} profile. Agent off / #stop to halt; Agent debug for "
            "per-tick goals.", "#66cc66")
        # Prime attack-form data so the sweep can pick the levelling form.
        if self.guild.lower() == "changelings":
            self.send_line("forms")
        self._agent_reschedule(0.6)

    def _agent_stop(self, why=""):
        self._agent_on = False
        self._agent_homing = False
        self._agent_goal = ""
        if self._agent_job:
            self.root.after_cancel(self._agent_job)
            self._agent_job = None
        # Stop any sub-engine the Agent was driving so they don't keep moving.
        if self._explore_on:
            self._explore_stop("agent stopped")
        if self._bot_on:
            self._bot_stop("agent stopped")
        self.write_local(f"[agent] {why or 'off'}.", "#aa88cc")

    def _agent_reschedule(self, secs):
        if self._agent_job:
            self.root.after_cancel(self._agent_job)
        self._agent_job = self.root.after(int(secs * 1000), self._agent_tick)

    def _agent_unknown_stubs(self):
        if self.mapdb is None or self._agent_area_id is None:
            return 0
        try:
            return len(self.mapdb.stubs_in_area(self._agent_area_id))
        except Exception:
            return 0

    # --- the loop: deadman -> combat survival -> phase machine -------------
    def _agent_tick(self):
        self._agent_job = None
        if not self._agent_on:
            return
        if not (self.conn and self.conn.alive):
            self._agent_stop("disconnected"); return
        # Deadman is law: stop the sub-engines and walk home, then stop. The
        # Agent never keeps deadman alive (its sends are non-manual anyway).
        if self.deadman_tripped:
            if not self._agent_homing:
                self._agent_homing = True
                self._agent_goal = "home"
                if self._explore_on:
                    self._explore_stop("agent: deadman")
                if self._bot_on:
                    self._bot_stop("agent: deadman")
                self.write_local("[agent] deadman tripped - returning to start "
                                 "room, then stopping.", "#ffaa33")
                self._agent_home_step()
            return
        # Combat survival overlay (any phase): the sub-engine pauses in combat,
        # so the Agent owns heal/flee here.
        if self.in_combat:
            self._agent_goal = "survive"
            if self._agent_debug:
                self.write_local(
                    f"[agent] survive hp={self._agent_hp_pct()}% "
                    f"pools={self._agent_pool_pct()}%", "#6699cc")
            self._agent_combat_survival()
            self._agent_reschedule(self._agent_s("agent_combat_ms", 1200))
            return
        if self._agent_phase == "chart":
            self._agent_chart_tick(); return
        if self._agent_phase == "gate":
            self._agent_gate_tick(); return
        self._agent_sweep_tick()

    def _agent_room_count(self):
        if self.mapdb is None or self._agent_area_id is None:
            return 0
        try:
            return self.mapdb.conn.execute(
                "SELECT COUNT(*) c FROM rooms WHERE area_id=?",
                (self._agent_area_id,)).fetchone()["c"]
        except Exception:
            return 0

    def _agent_chart_tick(self):
        """Phase 1: chart the area via the Explore engine, then advance to the
        safety gate (or, when re-charting after Hunt boxed itself in, straight
        back to the sweep). Also entered mid-run to extend the map."""
        self._agent_goal = "chart"
        if self._explore_on:
            self._agent_reschedule(1.0); return        # let Explore run
        if not self._agent_explore_started:
            # Kick off Explore. It sets _explore_on synchronously when it takes;
            # if it didn't (transient location/area hiccup), retry a few times
            # rather than silently skipping the whole chart phase.
            if not self._agent_recharting:
                self.write_local("[agent] phase 1/2: charting the area.",
                                 "#6699cc")
            self.cmd_explore("")
            if self._explore_on:
                self._agent_explore_started = True
            else:
                self._agent_explore_retries += 1
                if self._agent_explore_retries > 3:
                    self._agent_explore_started = True   # give up; fall through
                    self.write_local("[agent] couldn't start charting here.",
                                     "#cc9933")
            self._agent_reschedule(1.5); return
        # Explore has finished (or we gave up starting it).
        if self._agent_recharting:
            self._agent_recharting = False
            if self._agent_room_count() <= self._agent_prechart_rooms:
                self._agent_stop("area swept - remaining stubs unreachable "
                                 "(special exits?)")
                return
            self.write_local(f"[agent] charted more ("
                             f"{self._agent_room_count()} rooms) - resuming "
                             "hunt.", "#66cc66")
            self._agent_explore_started = False
            self._agent_phase = "sweep"             # gate already passed
            self._agent_reschedule(0.4); return
        left = self._explore_stub_count()
        if self._agent_mode == "explore":
            self._agent_stop(f"charted '{self._agent_area_name}'"
                             + (f" ({left} stub(s) unreachable)" if left else ""))
            return
        self._agent_phase = "gate"
        self._agent_reschedule(0.3)

    def _agent_gate_tick(self):
        """Between charting and hunting: is this area safe? Compare the area's
        top monster class (from rating) to this character's best kill (plain
        score). Changeling-scoped for now; other guilds pass straight through."""
        self._agent_goal = "gate"
        if self.guild.lower() != "changelings":
            self._agent_phase = "sweep"
            self._agent_reschedule(0.3); return
        if self._agent_gate_until == 0.0:
            self.changeling_best_kill = None
            self.send_line("score")             # refresh best kill
            self.send_line("forms")             # refresh attack-form data
            self._agent_gate_until = time.time() + self._agent_s(
                "agent_gate_timeout_ms", 5000)
            self.write_local("[agent] safety check: reading score/rating...",
                             "#6699cc")
            self._agent_reschedule(0.5); return
        top, best = self.area_class_top, self.changeling_best_kill
        if best is None and time.time() < self._agent_gate_until:
            self._agent_reschedule(0.4); return     # still waiting for score
        if top is not None and best is not None:
            if top > best:
                self._agent_stop(
                    f"safety gate: area top class {top:,} > your best kill "
                    f"{best:,} - too tough, not hunting here")
                return
            self.write_local(f"[agent] safety gate OK: area top {top:,} <= best "
                             f"kill {best:,}.", "#66cc66")
        else:
            self.write_local(f"[agent] safety gate: incomplete data (area "
                             f"top={top}, best kill={best}) - proceeding with "
                             "caution.", "#cc9933")
        self._agent_phase = "sweep"
        self._agent_reschedule(0.3)

    def _agent_sweep_tick(self):
        """Phase 2: hunt the area via the Hunt engine, keeping ourselves in the
        levelling form between fights. Survival is handled in the combat
        overlay; Hunt handles movement, markers, party-skip and deadman."""
        self._agent_goal = "hunt"
        if not self._bot_on:
            if self._agent_hunt_started:
                # Hunt stopped on its own. The usual cause is a room with no
                # charted same-area exit (its exits are still unwalked stubs):
                # the area isn't fully charted here. If open stubs remain,
                # re-chart from this spot to extend the map, then resume.
                if self._agent_unknown_stubs() > 0:
                    self._agent_recharting = True
                    self._agent_prechart_rooms = self._agent_room_count()
                    self._agent_explore_started = False
                    self._agent_explore_retries = 0
                    self._agent_hunt_started = False
                    self._agent_phase = "chart"
                    self.write_local(
                        "[agent] hunt boxed in - charting more of "
                        f"'{self._agent_area_name}' "
                        f"({self._agent_unknown_stubs()} stub(s) open), then "
                        "resuming.", "#cc9933")
                    self._agent_reschedule(0.5); return
                self._agent_stop("area swept - no more reachable rooms"); return
            self._agent_hunt_started = True
            self.write_local("[agent] phase 2/2: sweeping (hunting).", "#6699cc")
            self._bot_start("hunt")
            self._agent_reschedule(1.0); return
        self._agent_ensure_form()
        self._agent_reschedule(self._agent_s("agent_tick_ms", 800))

    def _agent_ensure_form(self):
        now = time.time()
        if now - self._agent_form_at < self._agent_s("agent_form_cooldown_ms",
                                                     8000):
            return
        cmd = self.agent_hook("ensure_form", default=None)
        if cmd:
            self._agent_form_at = now
            self.write_local(f"[agent] {cmd}", "#88aacc")
            self.send_line(cmd)

    def _agent_combat_survival(self):
        now = time.time()
        flee_pct = int(self.setting("agent_flee_pct", 30))
        heal_pct = int(self.setting("agent_heal_pct", 90))
        if self.agent_hook("should_flee",
                           default=self._agent_hp_pct() < flee_pct) \
                and now - self._agent_flee_at > self._agent_s(
                    "agent_flee_cooldown_ms", 4000):
            self._agent_flee_at = now
            self._agent_flee()
            return
        if self.agent_hook("should_heal",
                           default=self._agent_hp_pct() < heal_pct) \
                and now - self._agent_heal_at > self._agent_s(
                    "agent_heal_cooldown_ms", 3000):
            self._agent_heal_at = now
            losing = self._agent_hp_pct() < int(
                self.setting("agent_triceratops_pct", 50))
            cmds = self.agent_hook("low_hp", losing,
                                   default=self._agent_default_heal())
            for c in (cmds or []):
                self.write_local(f"[agent] heal: {c}", "#88aacc")
                self.send_line(c)

    def _agent_default_heal(self):
        c = (self.setting("agent_heal_command", "") or "").strip()
        return [c] if c else []

    def _agent_flee(self):
        cmd = (self.setting("agent_flee_command", "") or "").strip()
        if cmd:
            self.write_local(f"[agent] flee: {cmd}", "#ffaa33")
            self.send_line(cmd)
            return
        e = self._agent_pick_exit()
        if e is None:
            self.write_local("[agent] flee: no usable exit from here!",
                             "#cc6666")
            return
        self.write_local(f"[agent] flee: leaving via {e['direction']}",
                         "#ffaa33")
        self._bot_send_move(e)

    def _agent_pick_exit(self):
        v = self._sql_cur_vnum
        if self.mapdb is None or v is None:
            return None
        cands = [e for e in self.mapdb.iter_exits(v)
                 if e["to_vnum"] is not None and not e["wait_seconds"]]
        return random.choice(cands) if cands else None

    def _agent_home_step(self):
        """Post-deadman walk home: re-pathfind each step and send ONE edge via
        _raw_send (the sanctioned deadman bypass, like _bot_home_step). Pauses
        for combat; stops once home or if no path."""
        self._agent_job = None
        if not (self._agent_on and self._agent_homing):
            return
        if not (self.conn and self.conn.alive):
            self._agent_stop("disconnected during return"); return
        if self.in_combat:
            self._agent_job = self.root.after(1000, self._agent_home_step)
            return
        cur, home = self._sql_cur_vnum, self._agent_start_vnum
        if cur is None:
            self._agent_job = self.root.after(1500, self._agent_home_step)
            return
        if home is None or cur == home:
            self._agent_stop("deadman - back at start room"); return
        edges = self.mapdb.find_path(cur, home)
        if not edges:
            self._agent_stop(f"deadman - no path back to start from {cur}")
            return
        for c in self._edge_cmds(edges[0]):
            self._raw_send(c)                   # bypasses the deadman gate
        self._agent_job = self.root.after(
            int(self._bot_move_s() * 1000), self._agent_home_step)

    def _area_name(self, area_id):
        if area_id is None or self.mapdb is None:
            return "?"
        row = self.mapdb.conn.execute(
            "SELECT name FROM areas WHERE area_id=?", (area_id,)).fetchone()
        return row["name"] if row and row["name"] else "?"

    def _bot_area_of(self, vnum):
        if self.mapdb is None or vnum is None:
            return None
        row = self.mapdb.get_room(vnum)
        return row["area_id"] if row else None

    def _bot_start(self, mode="aggro", mapless=False, fence_vnum=None, fence_dir=None):
        verb = "bot" if mode == "aggro" else "hunt"
        v = None
        area_id = None
        if not mapless:
            if self.map_backend != "sqlite" or self.mapdb is None:
                self.write_local(f"[{verb}] needs the sqlite map backend "
                                 f"(or '{verb.title()} mapless' in an "
                                 "uncartographable zone).", "#cc6666")
                return
            v = self._sql_cur_vnum
            if v is None:
                self.write_local(f"[{verb}] location unknown - move a room "
                                 f"so the map locates you, then "
                                 f"{verb.title()}.", "#cc6666")
                return
            area_id = self._bot_area_of(v)
            if area_id is None:
                self.write_local(f"[{verb}] this room has no charted area - "
                                 "can't scope the hunt. Map it first.",
                                 "#cc6666")
                return
        self._bot_on = True
        self._bot_mode = mode
        self._bot_mapless = mapless
        self._bot_last_dir = None
        self._bot_area_id = area_id
        self._bot_area_name = self._area_name(area_id) if area_id else ""
        self._bot_start_vnum = v
        self._bot_prev_vnum = None
        self._bot_fence_vnum = fence_vnum
        self._bot_fence_dir = fence_dir
        self._bot_in_combat = self.in_combat
        self._bot_combat_end = 0.0
        self._bot_homing = False
        self._bot_home_reason = ""
        self._bot_no_combat_rooms = 0
        self._bot_recheck = True        # first tick looks at the current room
        self._hunt_attacked = False
        self._hunt_whiffs = 0
        self._hunt_lines = []
        self._bot_waiting_room = False
        self._bot_room_ready = False
        self._bot_room_mob = self._bot_room_player = False
        self._bot_mob_lines = []
        self.send_line("pwho")          # seed the party whitelist
        self._bot_pwho_at = time.time()
        markers = ("reads the room's mob (italic) / player (underline) "
                   "markers - set 'aset look_monster italics' and "
                   "'aset look_player underline' on the MUD")
        where = (f"area '{self._bot_area_name}'" if not mapless
                 else "blind (mapless - no map, no area)")
        home = ("stops in place" if mapless else "walks back to start")
        if mode == "hunt":
            aa = (self.setting("autoattack_command", "") or "").strip()
            how = (f"autoattack '{aa}'" if aa else "kill <keyword>")
            limit = self.wander_clear_limit if mapless else self.hunt_clear_limit
            clear = (f", or {limit} rooms with no combat," if limit else "")
            self.write_local(
                f"[hunt] roaming {where} - {markers}; "
                f"attacks any mob it finds via {how}, skips rooms with a "
                f"non-party player. Hunt off to stop. (Needs autocombat ON; "
                f"on deadman{clear} it {home}.)",
                "#66cc66")
        else:
            self.write_local(
                f"[bot] roaming {where} - {markers}; "
                "fights aggro as it comes, skips rooms with a non-party "
                f"player. Bot off to stop. (Needs autocombat ON; on deadman "
                f"it {home}.)",
                "#66cc66")
        self._bot_reschedule(0.6)

    def _bot_stop(self, why=""):
        verb = "hunt" if self._bot_mode == "hunt" else "bot"
        self._bot_on = False
        self._bot_homing = False
        self._bot_home_reason = ""
        self._bot_recheck = False
        self._bot_waiting_room = False
        self._bot_room_ready = False
        self._hunt_attacked = False
        self._hunt_whiffs = 0
        self._bot_fence_vnum = None
        self._bot_fence_dir = None
        if self._bot_job:
            self.root.after_cancel(self._bot_job)
            self._bot_job = None
        self.write_local(f"[{verb}] {why or 'off'}.", "#aa88cc")

    def _bot_reschedule(self, secs):
        if self._bot_job:
            self.root.after_cancel(self._bot_job)
        self._bot_job = self.root.after(int(secs * 1000), self._bot_tick)

    def _bot_tick(self):
        self._bot_job = None
        if not self._bot_on:
            return
        if not (self.conn and self.conn.alive):
            self._bot_stop("disconnected"); return
        # --- deadman: finish any fight, then walk back to the start room
        # and stop. The bot does NOT keep deadman alive - that's the whole
        # safety: idle keyboard -> trip -> come home. ---
        if self.deadman_tripped:
            if self.in_combat:
                self._bot_reschedule(1.0); return   # don't leave mid-fight
            if self._bot_mapless:
                self._bot_stop_in_place("deadman tripped"); return
            if not self._bot_homing:
                self._bot_homing = True
                self._bot_home_reason = ("deadman tripped - returning to "
                                         "start room, then stopping.")
                self.write_local(f"[bot] {self._bot_home_reason}", "#ffaa33")
                self._bot_home_step()
            return                                  # _bot_home_step drives now
        if self._sql_cur_vnum is None:
            self._bot_reschedule(2.0); return
        # --- combat gating: never move while engaged ---
        if self.in_combat:
            self._bot_in_combat = True
            self._hunt_attacked = False     # the swing connected; the fight is
            self._hunt_whiffs = 0           # now tracked by the post-kill path
            self._bot_reschedule(1.0); return
        if self._bot_in_combat:                 # fight just ended
            self._bot_in_combat = False
            self._bot_combat_end = time.time()
            self._bot_no_combat_rooms = 0        # a fight happened - area's still live
            self._bot_recheck = True            # re-look THIS room for more mobs
            self._arm_post_kill_resume()        # guild revalrie hold (if set)
            self._bot_reschedule(self._bot_postcombat_s()); return
        if self._bot_recheck and (
                time.time() - self._bot_combat_end < self._bot_postcombat_s()
                or self._post_kill_holding()):
            self._bot_reschedule(0.3); return   # loot/harvest/revalrie first
        # --- area boundary (mapless has no area concept - skip) ---
        if not self._bot_mapless and \
                self._bot_area_of(self._sql_cur_vnum) != self._bot_area_id:
            self._bot_stop(f"left area '{self._bot_area_name}' - "
                           "re-Bot where you want to hunt"); return
        # --- (re)look the current room: start, or after a kill ---
        if self._bot_recheck:
            self._bot_recheck = False
            self._bot_enter_room(refresh=True); return
        # --- assess the room from its markers, once the display is in ---
        if self._bot_waiting_room and not self._bot_room_ready:
            # safety fallback: neither prompt nor packet fired in time.
            self._bot_room_ready = True
            self._bot_ready_at = time.time()
            if self._bot_debug:
                self._bot_debug_report("SAFETY-TIMER")
        if self._bot_room_ready:
            if self._bot_assess() != "move":
                return                          # assess rescheduled itself
        # --- pick and send the next roam step ---
        if self._bot_mapless:
            d = self._bot_pick_move_mapless()
            if d is None:
                self._bot_stop("no exit from here"); return
        else:
            e = self._bot_pick_move()
            if e is None:
                self._bot_stop(f"no charted exit within "
                               f"'{self._bot_area_name}' from here"); return
        # Hunt-only area-cleared safety net: this room contributed no combat
        # (we're about to leave it). Bot (aggro) stays an unbounded passive
        # roam, untouched. Reuses the deadman walk-home (_bot_home_step,
        # which re-pathfinds via mapdb.find_path every step) rather than
        # recording a reverse trail - that's what made the now-removed Run
        # engine's version of this fragile.
        if self._bot_mode == "hunt":
            self._bot_no_combat_rooms += 1
            limit = (self.wander_clear_limit if self._bot_mapless
                     else self.hunt_clear_limit)
            if limit and self._bot_no_combat_rooms >= limit:
                reason = (f"area cleared - no combat in "
                         f"{self._bot_no_combat_rooms} rooms")
                if self._bot_mapless:
                    self._bot_stop_in_place(reason); return
                self._bot_home_reason = (
                    f"{reason}, returning to start room, then stopping.")
                self._bot_homing = True
                self.write_local(f"[hunt] {self._bot_home_reason}", "#ffaa33")
                self._bot_home_step()
                return
        self._bot_prev_vnum = self._sql_cur_vnum
        if self._bot_mapless:
            self._bot_send_move_mapless(d)
        else:
            self._bot_send_move(e)
        self._bot_enter_room(refresh=False)     # entry auto-look carries markers

    def _bot_enter_room(self, refresh):
        """Begin a room visit: clear the per-room marker flags and wait for
        the display to land (the prompt sets _bot_room_ready). A normal move
        gets the markers for free from the entry auto-look; start / post-combat
        rechecks send an explicit look (refresh=True) to re-read the room."""
        self._bot_room_mob = self._bot_room_player = False
        self._bot_mob_lines = []
        self._hunt_lines = []
        self._bot_minimap = {}
        self._mm_pending = []
        self._mm_south_col = None
        self._mm_south_count = 0
        self._hunt_attacked = False     # any fresh look re-evaluates the room
        if not refresh:
            self._hunt_whiffs = 0       # new room: fresh whiff budget
        self._bot_room_ready = False
        self._bot_waiting_room = True
        self._bot_ready_at = 0.0
        self._bot_room_t0 = time.time()
        self._bot_first_line_at = 0.0
        if refresh:
            self.send_line(self.setting("bot_refresh_command", "glance"))
        self._bot_reschedule(self._bot_safety_s())

    def _bot_on_prompt(self):
        """GA/EOR prompt ended the line - one signal that the room display is
        complete (see also _bot_on_room_packet)."""
        if not self._bot_on or self._bot_homing:
            return
        if self._pwho_capturing:
            self._finish_pwho()
        self._bot_room_displayed("prompt")

    def _bot_on_room_packet(self):
        """The room MIP packet (BAD/DDD) is the other completion signal, and on
        3s it lands right after the room text - BEFORE a GA that the server may
        hold until its next tick. Whichever arrives first marks the room ready,
        so the bot never waits a tick for a laggy prompt."""
        if self._bot_on and not self._bot_homing:
            self._bot_room_displayed("packet")

    def _bot_room_displayed(self, src):
        """Room display complete: the markers are all in, so mark ready and
        assess now (reschedule(0)). First of prompt/packet to fire wins."""
        if self._bot_waiting_room and not self._bot_room_ready:
            self._bot_room_ready = True
            self._bot_ready_at = time.time()
            if self._bot_debug:
                self._bot_debug_report(src)
            self._bot_reschedule(0)

    def _bot_debug_report(self, src):
        """Per-room timing split: wire = move sent -> first room line arrived
        (server round-trip); client = first line -> room ready (our parse +
        render + scheduling). Pinpoints whether a slow room is the MUD or us."""
        total = self._bot_ready_at - self._bot_room_t0
        if self._bot_first_line_at:
            wire = self._bot_first_line_at - self._bot_room_t0
            client = self._bot_ready_at - self._bot_first_line_at
            split = f"(wire {wire:.2f} + client {client:.2f})"
        else:
            split = "(no room line seen)"
        mm = f" minimap={self._bot_minimap}" if self._bot_mode == "hunt" else ""
        self.write_local(
            f"[bot] ready in {total:.2f}s {split} via {src}; "
            f"mob={self._bot_room_mob} player={self._bot_room_player}{mm}",
            "#6699cc")

    def _bot_assess(self):
        """Decide what to do in the just-displayed room. Returns "move" (caller
        roams on) or anything else (this method already rescheduled the tick)."""
        if self._bot_room_player:               # non-party player -> cede it
            self.write_local("[bot] player in room - moving on.", "#cc9933")
            return "move"
        if self._bot_room_mob:
            if self._bot_mode == "hunt":        # actively engage
                # We only reach here OUT of combat. If we already swung at this
                # mob, the fight just hasn't registered yet (3s can lag a beat
                # before the first hit lands) - autocombat finishes the mob once
                # engaged, so WAIT for combat to start; never roam off a live
                # mob. The tick's in_combat block clears _hunt_attacked once the
                # fight is real, and the post-kill re-look moves us on. Only if
                # combat never starts within the grace did the keyword whiff.
                grace = max(1.0, self.setting("hunt_engage_grace_ms", 4000)
                            / 1000.0)
                if self._hunt_attacked:
                    if time.time() - self._hunt_attacked_at < grace:
                        self._bot_reschedule(0.4)   # poll for combat to start
                        return "wait"
                    self._hunt_attacked = False      # never engaged -> whiffed
                cap = int(self.setting("hunt_max_whiffs", 5))
                if self._hunt_whiffs >= cap:
                    self.write_local(
                        f"[hunt] couldn't engage the mob after {cap} tries "
                        "(keyword?) - moving on.", "#cc9933")
                    return "move"
                if self._hunt_try_attack():
                    self._hunt_attacked = True
                    self._hunt_attacked_at = time.time()
                    self._hunt_whiffs += 1
                    self._bot_reschedule(self._hunt_settle_s())
                    return "attack"
                return "move"                   # nothing attackable here
            # aggro mode: give an aggressive mob a beat to bite, then move on
            if (not self.in_combat
                    and time.time() - self._bot_ready_at
                    < self._bot_aggro_wait_s()):
                self._bot_reschedule(0.3)
                return "wait"
            return "move"
        return "move"                           # empty room

    def _bot_move_s(self):
        return max(0.3, self.setting("bot_move_ms", 1500) / 1000.0)

    def _bot_safety_s(self):
        return max(0.8, self.setting("bot_safety_ms", 2500) / 1000.0)

    def _bot_aggro_wait_s(self):
        return max(0.0, self.setting("bot_aggro_wait_ms", 1500) / 1000.0)

    def _bot_postcombat_s(self):
        return max(0.0, self.setting("bot_postcombat_ms", 2500) / 1000.0)

    # ---- party whitelist (pwho roster + manual bot_party_whitelist) -------
    def _bot_whitelist(self):
        """Lowercase set of names the bot treats as friendly: the manual
        bot_party_whitelist setting + the last pwho roster + the player."""
        names = {str(n).strip().lower()
                 for n in (self.setting("bot_party_whitelist", []) or [])
                 if str(n).strip()}
        names |= self._bot_party
        if self.character:
            names.add(self.character.lower())
        names.discard("")
        return names

    def _line_is_party(self, clean):
        """True if any word of an underlined (player) line is a whitelisted
        name. Word-based (not first-token) so it survives pretitles."""
        wl = self._bot_whitelist()
        if not wl:
            return False
        toks = {re.sub(r"[^a-z0-9']", "", t.lower()) for t in clean.split()}
        return bool(toks & wl)

    def _scan_pwho(self, clean):
        """Capture the `pwho` roster: between the 'Name ... Creator' header
        and the trailing blank line, each line's first token is a member."""
        s = clean.rstrip()
        if not self._pwho_capturing:
            ls = s.lstrip()
            if ls.startswith("Name") and "Location" in s and "Creator" in s:
                self._pwho_capturing = True
                self._pwho_buf = []
            return
        if not s.strip():                       # blank line ends the table
            self._finish_pwho(); return
        tok = s.split()[0].strip().lower()
        if tok:
            self._pwho_buf.append(tok)

    def _finish_pwho(self):
        if self._pwho_buf:
            self._bot_party = set(self._pwho_buf)
        self._pwho_capturing = False
        self._pwho_buf = []

    # ---- post-kill resume gate (guild setting, shared by all bot engines) --
    def _arm_post_kill_resume(self):
        """Call when a fight ends. If the active guild defines
        `post_kill_resume_text` (e.g. bladesinger revalrie) and/or
        `post_kill_resume_vitals` (e.g. viking vrest, which has no end text -
        see _vitals_topped_up), arm a hold so the bot waits before resuming.
        Neither setting -> no-op."""
        txt = (self.setting("post_kill_resume_text", "") or "").strip()
        vitals = self.setting("post_kill_resume_vitals", None) or []
        if not txt and not vitals:
            return
        # The text/vitals dip may only happen solo (e.g. bladesinger revalrie
        # is cast by the noparty kill-trigger). If so, don't arm in a party or
        # we'd wait the full timeout for a release condition that never comes.
        if self.setting("post_kill_resume_noparty_only", False) \
                and self.party_on():
            return
        self._resume_text = txt
        self._resume_vitals = list(vitals)
        self._resume_vitals_margin = float(
            self.setting("post_kill_resume_vitals_margin", 20))
        self._resume_pending = True
        self._resume_deadline = time.time() + max(
            1, int(self.setting("post_kill_resume_timeout", 90)))

    def _vitals_topped_up(self):
        """True once every field named in self._resume_vitals is within
        self._resume_vitals_margin of its tracked max. A field with no known
        cur/max yet doesn't block (can't wait on data that never arrived)."""
        for f in self._resume_vitals:
            cur = self.vitals.get(f)
            top = self.vitals.get(f + "max") or self.vitals_max.get(f, 0)
            if cur is None or not top:
                continue
            if top - cur > self._resume_vitals_margin:
                return False
        return True

    def _post_kill_holding(self):
        """True while a bot should keep waiting for the post-kill resume
        condition (resume text and/or topped-up vitals). Clears on arrival
        (text: poll_queue via _resume_scan_line; vitals: checked here on every
        poll) or on timeout (here)."""
        if not self._resume_pending:
            return False
        if self._resume_vitals and self._vitals_topped_up():
            self._resume_pending = False
            return False
        if time.time() >= self._resume_deadline:
            self._resume_pending = False
            self.write_local("[bot: resume condition never came - resuming]",
                             "#888899")
            return False
        return True

    def _resume_scan_line(self, clean):
        """One output line: if we're holding for the post-kill resume text and
        it just arrived, release the hold."""
        if self._resume_pending and self._resume_text \
                and self._resume_text in clean:
            self._resume_pending = False

    def _bot_scan_markers(self, spans, clean):
        """One room line, while waiting for the room display: italic -> a mob
        is here (keep the line for a kill keyword); underline from a non-party
        name -> a player is here. Read on the prompt by _bot_assess."""
        if not self._bot_first_line_at and clean.strip():
            self._bot_first_line_at = time.time()    # wire RTT marker
        ital, under = self._line_markers(spans)
        if ital:
            self._bot_room_mob = True
            self._bot_mob_lines.append(clean)
            self._bot_mob_lines = self._bot_mob_lines[-10:]
        elif under and not self._line_is_party(clean):
            self._bot_room_player = True
        self._hunt_lines.append(clean)          # legacy keyword fallback
        self._hunt_lines = self._hunt_lines[-40:]
        if self._bot_mode == "hunt":
            self._mm_scan_line(spans, clean)

    # ---- Hunt-only: 3s minimap cross parsing (radius-2 N/S/E/W + dead-end
    # boundary marker "#") ---------------------------------------------------
    # Dashes flank a N/S cell only when that cell itself has its own extra
    # E/W exit dangling off the spine - a cell without one renders with no
    # dash on that side at all (confirmed live: a N1 cell with no west
    # exit of its own rendered "O-", not "-O-"), so both sides are
    # independently optional rather than required.
    _MM_SINGLE_RE = re.compile(r"^\s*[#-]?([O1-9*])-?\s*$")
    # near/far: "mob" | "player" | None (empty/unclassified/not rendered).
    # deadend: True only if a "#" boundary token was seen on that side -
    # confirms there's nothing further out, vs. simply not having rendered
    # that far (which leaves deadend False, i.e. "unknown beyond here").
    _MMCell = namedtuple("_MMCell", "near far deadend")

    @staticmethod
    def _mm_span_tag_at(spans, col):
        """The tag covering character index `col` of a line's spans."""
        pos = 0
        for text, tag in spans:
            if col < pos + len(text):
                return tag
            pos += len(text)
        return None

    def _mm_classify(self, ch, spans, col):
        """A minimap cell char -> "mob" / "player" / None (empty/unknown)."""
        if ch == "O":
            return None
        if ch not in "123456789*":
            return None
        tag = self._mm_span_tag_at(spans, col)
        fg = None
        if tag:
            for part in tag.split("_")[1:]:
                if part.startswith("f") and len(part) > 1:
                    fg = int(part[1:])
        if fg in (35, 95):
            return "mob"
        if fg in (32, 92):
            return "player"
        return None          # unrecognized color - treat as neutral

    _MM_KIND_RANK = {None: 0, "player": 1, "mob": 2}

    def _mm_scan_side(self, clean, spans, idx, sign):
        """Walk outward from @ (index idx) in steps of 2, direction `sign`
        (-1 west, +1 east): depth 0 (offset 2) is 'near'. Originally
        capped at depth 1 ('far', offset 4) on the assumption the wire
        never renders past radius-2 - confirmed false live: a room with no
        exit on the opposite side (e.g. "(w,n,s)", no east) extends this
        side past radius-2 with the row width freed up by the empty side,
        and a real mob showed up there. So this keeps walking - depth 1+
        all fold into 'far', with a mob/player signal at any of those
        depths beating a plain empty one found at another depth (a mob
        three rooms out is still worth biasing toward; there's no reason
        to let a closer empty reading hide it). A "#" at any depth ends
        the walk and marks a confirmed charted dead end; any other
        non-cell character (blank padding, or running off the line) ends
        it with deadend left False. Returns None if nothing usable was
        found on this side at all."""
        near = far = None
        deadend = False
        depth = 0
        while True:
            pos = idx + sign * 2 * (depth + 1)
            if not (0 <= pos < len(clean)):
                break
            ch = clean[pos]
            if ch == "#":
                deadend = True
                break
            if ch != "O" and ch not in "123456789*":
                break
            kind = self._mm_classify(ch, spans, pos)
            if depth == 0:
                near = kind
            elif far is None or self._MM_KIND_RANK[kind] > \
                    self._MM_KIND_RANK[far]:
                far = kind
            depth += 1
        if near is None and far is None and not deadend:
            return None
        return self._MMCell(near, far, deadend)

    def _mm_scan_line(self, spans, clean):
        """Feed one room-display line through the minimap cross parser.
        West/east come straight off the @ row (read outward in both
        directions, radius-1 then radius-2); north/south are the nearest
        two lone "-X-" rows immediately before/after it (shape-matched, not
        a fixed line offset, since the cross can render 4 or 5 columns
        wide). See docs/superpowers/specs/2026-06-18-hunt-minimap-design.md
        (radius-1 design) - radius-2 + the "#" dead-end marker extend it.
        Vertical (N/S) dead-end detection is deliberately not attempted: no
        captured example confirms where/whether "#" renders there, so N/S
        cells always report deadend=False (unknown, never wrongly so)."""
        idx = clean.find("@")
        if idx >= 0:
            # A missing exit renders as blank, not a dash (no "-" there at
            # all) - so west/east are each gated on their OWN dash, not on
            # each other. This must trigger on '@' alone, not "and has a
            # dash on at least one side": a room with no e/w exits at all
            # (e.g. "(n,s)") has @ flanked by blank on both sides, and
            # still needs this branch to run so north's pending capture
            # gets drained into _bot_minimap and south gets (re-)armed for
            # *this* room - otherwise both stayed stuck on whatever the
            # previous room (that did have an e/w exit) last left there.
            has_w = idx > 0 and clean[idx - 1] == "-"
            has_e = idx < len(clean) - 1 and clean[idx + 1] == "-"
            if has_w:
                w = self._mm_scan_side(clean, spans, idx, -1)
                if w is not None:
                    self._bot_minimap["w"] = w
            if has_e:
                e = self._mm_scan_side(clean, spans, idx, +1)
                if e is not None:
                    self._bot_minimap["e"] = e
            pend = [p for p in self._mm_pending if p[0] == idx]
            if pend:
                col, ch, sp = pend[-1]
                near = self._mm_classify(ch, sp, col)
                far = None
                if len(pend) > 1:
                    col2, ch2, sp2 = pend[-2]
                    far = self._mm_classify(ch2, sp2, col2)
                if near is not None or far is not None:
                    self._bot_minimap["n"] = self._MMCell(near, far, False)
            self._mm_pending = []
            self._mm_south_col = idx
            self._mm_south_count = 0
            return
        m = self._MM_SINGLE_RE.match(clean)
        if m:
            col = m.start(1)
            if self._mm_south_col is not None and col == self._mm_south_col \
                    and self._mm_south_count < 2:
                kind = self._mm_classify(m.group(1), spans, col)
                slot = "near" if self._mm_south_count == 0 else "far"
                cell = self._bot_minimap.get(
                    "s", self._MMCell(None, None, False))
                self._bot_minimap["s"] = cell._replace(**{slot: kind})
                self._mm_south_count += 1
                if self._mm_south_count >= 2:
                    self._mm_south_col = None
            elif self._mm_south_col is None:
                self._mm_pending = (
                    self._mm_pending + [(col, m.group(1), spans)])[-2:]

    def _bot_pick_move_mapless(self):
        """Plain mapless wander: pick a random direction from the CURRENT
        room's live exit list (self.exits, kept fresh every room regardless
        of charting), excluding the exact opposite of the last direction
        moved when another option exists (the closest mapless equivalent of
        the charted picker's anti-backtrack rule - there's no vnum here to
        compare against). Returns None if there are no exits at all (a real
        dead end, not a charting gap)."""
        exits = list(self.exits or [])
        if not exits:
            return None
        reverse = _DIR_OPPOSITE.get(self._bot_last_dir)
        forward = [d for d in exits if d != reverse]
        pool = forward or exits
        return random.choice(pool)

    def _bot_send_move_mapless(self, direction):
        self._bot_last_dir = direction
        self.send_line(direction)

    def _bot_pick_move(self):
        """A random charted exit whose destination is in the hunt area,
        preferring not to immediately backtrack. Skips timed (wait>0) and
        unmapped (to_vnum NULL) exits."""
        v = self._sql_cur_vnum
        cands = []
        for e in self.mapdb.iter_exits(v):
            to = e["to_vnum"]
            if to is None or e["wait_seconds"]:
                continue
            if self._bot_area_of(to) != self._bot_area_id:
                continue
            if self._bot_mode == "hunt" and self.hunt_flat \
                    and e["direction"] not in self.CARDINAL_DIRS:
                continue
            cands.append(e)
        if self._bot_fence_vnum and v == self._bot_fence_vnum:
            cands = [e for e in cands if e["direction"] != self._bot_fence_dir]
        if not cands:
            return None
        forward = [e for e in cands if e["to_vnum"] != self._bot_prev_vnum]
        pool = forward or cands
        if self._bot_mode != "hunt" or not self._bot_minimap:
            return random.choice(pool)
        weights = [self._mm_move_weight(e["direction"]) for e in pool]
        return random.choices(pool, weights=weights)[0]

    # mob cell (near or far - same weight, no distance tiering): walk toward
    # it. player cell: Hunt already eats a wasted step skipping a non-party
    # player on arrival, so don't walk into one on purpose. Confirmed dead
    # end with nothing at any captured depth: deprioritized, but never to
    # zero (random.choices errors on all-zero weights, and this format-sniff
    # is fragile enough that a hard exclusion would be unsafe - it stays
    # selectable as a last resort). No data (None - includes u/d/diagonals,
    # which this minimap never reports): unchanged neutral pick.
    _MM_KIND_WEIGHTS = {"mob": 6.0, "player": 0.25}
    _MM_DEADEND_EMPTY_WEIGHT = 0.15

    def _mm_move_weight(self, direction):
        cell = self._bot_minimap.get(direction)
        if cell is None:
            return 1.0
        for kind in (cell.near, cell.far):      # near wins over far
            if kind in self._MM_KIND_WEIGHTS:
                return self._MM_KIND_WEIGHTS[kind]
        if cell.deadend and cell.near is None and cell.far is None:
            return self._MM_DEADEND_EMPTY_WEIGHT
        return 1.0

    def _hunt_settle_s(self):
        return max(0.5, self.setting("hunt_settle_ms", 1800) / 1000.0)

    def _hunt_try_attack(self):
        """Engage a mob in the current room. Returns True if an attack was
        sent (caller waits to see if combat starts), False if there was
        nothing to attack (caller moves on).

        Primary path: the guild's `autoattack_command` setting (e.g. Gentech
        'autokill') - one command that targets whatever's in the room; we let
        in_combat tell us if it took. If the setting contains the token '{t}'
        (e.g. bards 'cacophony {t}', jedi 'focus force taunt on {t}'), it needs
        an explicit target: we take a kill keyword from the italic (look_monster)
        mob line, and if none can be found there's nothing to attack. Fallback
        (setting empty): `kill <keyword>` on that same keyword. Called only when
        a mob marker is present, so the keyword comes from a known mob line."""
        aa = (self.setting("autoattack_command", "") or "").strip()
        if aa:
            if "{t}" in aa:
                kw = self._bot_kill_keyword()
                if not kw:
                    return False
                self.send_line(aa.replace("{t}", kw))
            else:
                self.send_line(aa)
            return True
        kw = self._bot_kill_keyword()
        if kw:
            self.send_line(f"kill {kw}")
            return True
        return False

    def _bot_kill_keyword(self):
        """A kill keyword for the mob in the room. Prefers the italic
        (look_monster) lines captured this room - we KNOW those are mobs - and
        falls back to the legacy room-text heuristic if those came up empty."""
        for line in self._bot_mob_lines:
            kw = self._keyword_from_mob_line(line)
            if kw:
                return kw
        return self._hunt_parse_target()

    # Words that begin a POST-noun modifier in a creature's short description,
    # so the kill keyword is the noun just BEFORE them. Prepositions /
    # relativizers + the participles MUDs commonly use for "doing X". NOT
    # "and" - that joins adjectives ("a cute and fuzzy squirrel" -> squirrel).
    _MOB_NOUN_STOPWORDS = frozenset("""
        of in on at with from to near by behind beside beneath under over
        around against upon inside outside atop that who which whom whose here
        is are was were be been being
        holding wielding carrying wearing bearing testing examining guarding
        standing sitting lying kneeling leaning looking watching sleeping
        resting working eating drinking fighting riding floating hovering
        blocking patrolling wandering reading writing talking praying chanting
        muttering grumbling snarling growling prowling searching digging
        waiting hanging crawling perched covered made wrapped dressed clad
    """.split())

    def _keyword_from_mob_line(self, line):
        """Kill keyword from a known mob line: the noun at the END of the
        leading noun phrase (after a leading article, before any post-noun
        modifier). Drops bracket tags ('[wounded]'). So 'A cute and fuzzy
        Squirrel.' -> 'squirrel', while 'A scientist testing blunt prototypes
        [scratched].' -> 'scientist' (stops at the participle 'testing'). The
        old 'first word' rule mis-fired on adjective-prefixed mobs."""
        s = re.sub(r"\[[^\]]*\]", "", line).strip().rstrip(".")
        words = [w for w in (t.strip(",.;:") for t in s.split()) if w]
        if words and words[0].lower() in ("a", "an", "the", "some"):
            words = words[1:]
        if not words:
            return None
        keep = []
        for w in words:
            if w.lower() in self._MOB_NOUN_STOPWORDS:
                # A stopword only ends the noun phrase when we've already
                # banked a noun ('scientist testing ...' -> scientist). When
                # it LEADS the phrase ('a wandering tourist', 'a prowling
                # cat') it's an adjective, not a 'doing X' verb - skip it and
                # keep scanning for the real noun.
                if keep:
                    break
                continue
            keep.append(w)
        kw = keep[-1] if keep else (words[-1] if words else None)
        return kw.lower() if kw else None

    # Default room-creature line patterns (group 1 = the kill keyword), the
    # fallback when no look_monster marker line was captured. Override per
    # cascade with setting "mob_line_patterns" (list of regex, group 1 = kw).
    DEFAULT_MOB_PATTERNS = [
        r"^(?:A|An|The|Some)\s+.*?\b(\w+)\s+(?:is|are)\b.*\bhere\b",
    ]

    def _hunt_parse_target(self):
        """Best-effort: scan the buffered room text for a mob and return a
        kill keyword, or None. UNVERIFIED format - see DEFAULT_MOB_PATTERNS."""
        pats = self.setting("mob_line_patterns") or self.DEFAULT_MOB_PATTERNS
        compiled = []
        for p in pats:
            try:
                compiled.append(re.compile(p, re.I))
            except re.error:
                pass
        for line in self._hunt_lines:
            for rx in compiled:
                m = rx.search(line)
                if m and m.group(1):
                    return m.group(1).lower()
        return None

    @staticmethod
    def _edge_cmds(e):
        """One exit -> the command(s) to traverse it: setup_command (if any)
        then the special command or the bare direction. Mirrors
        _sql_move_plan / speedrun_string."""
        out = []
        if e["setup_command"]:
            out.append(e["setup_command"])
        direction, cmd = e["direction"], e["command"]
        out.append(cmd if (cmd and cmd != direction) else direction)
        return out

    def _bot_send_move(self, e):
        for c in self._edge_cmds(e):
            self.send_line(c)

    def _bot_stop_in_place(self, reason):
        """Mapless equivalent of walking home: there's no verified path back
        to anywhere (the temp-map's reciprocal edges, where one even exists,
        are an assumption, not a proof - see the design doc), so the safest
        thing on a deadman trip or the no-combat limit is to just stop right
        here rather than guess a route. Combat is never abandoned mid-fight -
        callers only invoke this once self.in_combat is already False."""
        self._bot_stop(f"{reason} (mapless - stopped in place, no map to "
                       "walk home through)")

    def _bot_home_step(self):
        """Walk-home return (deadman trip, or Hunt's area-cleared limit):
        re-pathfind to the start room each step (so a fight or any drift
        can't strand us) and send ONE edge. Uses _raw_send to bypass the
        deadman send-gate - this bounded walk-home is the sanctioned
        exception to deadman, not continuous botting. Pauses for combat,
        stops once home / if no path. _bot_home_reason (set by whichever
        trigger armed _bot_homing) supplies the stop message."""
        self._bot_job = None
        if not (self._bot_on and self._bot_homing):
            return
        if not (self.conn and self.conn.alive):
            self._bot_stop("disconnected during return"); return
        if self.in_combat:
            self._bot_job = self.root.after(1000, self._bot_home_step)
            return
        cur, home = self._sql_cur_vnum, self._bot_start_vnum
        if cur is None:
            self._bot_job = self.root.after(1500, self._bot_home_step)
            return
        if home is None or cur == home:
            self._bot_stop(self._bot_home_reason or "back at start room")
            return
        edges = self.mapdb.find_path(cur, home)
        if not edges:
            self._bot_stop(self._bot_home_reason or
                           f"no path back to start from {cur}")
            return
        for c in self._edge_cmds(edges[0]):
            self._raw_send(c)                   # bypasses the deadman gate
        self._bot_job = self.root.after(
            int(self._bot_move_s() * 1000), self._bot_home_step)

    def cmd_chaossea(self, arg):
        a = arg.strip().lower()
        if a in ("off", "stop", "halt"):
            if self._chaossea_on:
                self._chaossea_stop("stopped")
            else:
                self.write_local("Chaossea is not running.", "#cc9933")
            return
        if self._chaossea_on:
            self.write_local(
                "Chaossea already running. Chaossea off to stop.",
                "#cc9933")
            return
        if a and a not in ("fight", "fighting"):
            self.write_local(
                "Chaossea [fight] | Chaossea off - unrecognized arg "
                f"'{a}'.", "#cc9933")
            return
        self._chaossea_start(fight_mode=a in ("fight", "fighting"))

    def _chaossea_start(self, fight_mode=False):
        self._chaossea_on = True
        self._chaossea_fight_mode = fight_mode
        self._chaossea_map = {}
        self._chaossea_room_exits = {}
        self._chaossea_cur = "f00001"
        self._chaossea_next_id = 1
        self._chaossea_last_dir = None
        self._chaossea_has_charm = False
        self._chaossea_has_cube = False
        self._chaossea_room_mobs = 0
        self._chaossea_mob_idx = 0
        self._chaossea_examine_buf = []
        self._chaossea_examine_pending = False
        self._chaossea_waiting_room = True
        self._chaossea_room_ready = False
        self._chaossea_room_mob = False
        self._chaossea_room_player = False
        self._chaossea_mob_lines = []
        self._chaossea_pre_move_room = None
        self._chaossea_got_ddd = False
        self._chaossea_pre_move_mobs = 0
        self._chaossea_pending_loot = None
        self._chaossea_was_fighting = False
        self._chaossea_post_kill_wait = False
        self._chaossea_room_done = False
        self._chaossea_engage_at = 0.0
        self._chaossea_engage_whiffs = 0
        self._chaossea_exits_retry = 0
        self._chaossea_skip_ddds = 0
        self._chaossea_room_entered_at = time.time()
        if fight_mode:
            self.write_local(
                "[chaossea] fight mode - kills every mutant it meets, "
                "ignores drops, dives any 'down' exit on sight. Chaossea "
                "off to stop. (Needs autocombat ON.)", "#66cc66")
        else:
            self.write_local(
                "[chaossea] hunting for a chaotic charm + cube of raw chaos "
                "- examines every mutant, fights the ones carrying a needed "
                "item (or that aggro/block you), retreats once both are "
                "found. Chaossea off to stop. (Needs autocombat ON.)",
                "#66cc66")
        # Every later room display is the natural reply to Chaossea's own
        # previous move, but there is no previous move yet at start - so
        # without an explicit refresh here it just sits waiting for a
        # display that never spontaneously arrives (confirmed live: it
        # took a manual glance to get it moving at all). Same fix Bot uses
        # at its own start (_bot_enter_room(refresh=True)).
        if self.map_backend == "sqlite" and self._sql_charting:
            self.write_local(
                "[chaossea] WARNING: charting mode is ON - Chaossea's "
                "movement commands will be buffered by the chart gate and "
                "nothing will happen. Turn charting off first (#chart off).",
                "#cc6666")
            self._chaossea_stop(); return
        self.send_line(self.setting("bot_refresh_command", "glance"),
                       gated=False)
        self._chaossea_reschedule(0.6)

    def _chaossea_stop(self, why=""):
        self._chaossea_on = False
        if self._chaossea_job:
            self.root.after_cancel(self._chaossea_job)
            self._chaossea_job = None
        self.write_local(f"[chaossea] {why or 'off'}.", "#aa88cc")
        if self.map_backend == "sqlite":
            self.sql_set_status()   # unblock the map pane (guard was _chaossea_on)

    def _chaossea_reschedule(self, secs):
        if self._chaossea_job:
            self.root.after_cancel(self._chaossea_job)
        self._chaossea_job = self.root.after(
            int(secs * 1000), self._chaossea_tick)

    @staticmethod
    def _chaossea_mob_keyword(idx):
        """Every mob in Sea of Chaos IDs to the single keyword "mutant"
        regardless of its random short description (per the user) - so
        targeting never needs per-mob name parsing, only an ordinal suffix
        to disambiguate more than one in a room. PROVISIONAL: the exact
        ordinal syntax this MUD expects ("mutant 2" vs "2.mutant" etc.) is
        unconfirmed - adjust this one spot once verified live."""
        return "mutant" if idx <= 1 else f"mutant {idx}"

    def _chaossea_wants(self, has_charm, has_cube):
        """Which items are still worth examining/fighting for, given what
        we already have."""
        want = []
        if not has_charm:
            want.append("a chaotic charm")
        if not has_cube:
            want.append("a cube of raw chaos")
        return want

    def _chaossea_next_examine(self):
        """Send the next unchecked mutant's `examine`, or - once every
        mutant in the room has been checked - decide the room is done and
        let the tick move to the next room."""
        self._chaossea_mob_idx += 1
        if self._chaossea_mob_idx > self._chaossea_room_mobs:
            self._chaossea_room_done = True
            self._chaossea_reschedule(0)
            return
        self._chaossea_examine_buf = []
        self._chaossea_examine_pending = True
        kw = self._chaossea_mob_keyword(self._chaossea_mob_idx)
        self.send_line(f"examine {kw}")
        self._chaossea_reschedule(self._bot_safety_s())

    def _chaossea_next_kill(self):
        """Fight-mode equivalent of _chaossea_next_examine: no examine step
        at all, just attack the next unchecked mutant in ordinal order, or -
        once every mutant in the room is dead - mark the room done."""
        self._chaossea_mob_idx += 1
        if self._chaossea_mob_idx > self._chaossea_room_mobs:
            self._chaossea_room_done = True
            self._chaossea_reschedule(0)
            return
        self._chaossea_engage_mob(self._chaossea_mob_idx, [])

    def _chaossea_examine_scan_line(self, clean):
        if self._chaossea_examine_pending:
            self._chaossea_examine_buf.append(clean)

    def _chaossea_finish_examine(self):
        """The prompt after an `examine` landed - decide what to do with
        this mutant from the accumulated text, then either attack it or
        move on to the next one in the room."""
        self._chaossea_examine_pending = False
        text = " ".join(self._chaossea_examine_buf).lower()
        want = self._chaossea_wants(self._chaossea_has_charm,
                                    self._chaossea_has_cube)
        carries = [item for item in want if item in text]
        if carries:
            self._chaossea_engage_mob(self._chaossea_mob_idx, carries)
            return
        self._chaossea_next_examine()

    def _chaossea_engage_mob(self, idx, carrying):
        """Attack the mutant at this ordinal; Task 9 handles the post-kill
        loot + goal update once combat clears (same in_combat gating the
        tick already does for every other bot in this client)."""
        self._chaossea_pending_loot = carrying
        self._chaossea_engage_at = time.time()
        self._chaossea_engage_whiffs = 0
        kw = self._chaossea_mob_keyword(idx)
        self.send_line(f"kill {kw}")
        self._chaossea_reschedule(self._hunt_settle_s())

    def _chaossea_enter_room(self):
        self._chaossea_room_mob = self._chaossea_room_player = False
        self._chaossea_mob_lines = []
        self._chaossea_room_mobs = 0
        self._chaossea_mob_idx = 0
        self._chaossea_examine_buf = []
        self._chaossea_examine_pending = False
        self._chaossea_room_ready = False
        self._chaossea_waiting_room = True
        self._chaossea_exits_retry = 0
        self._chaossea_room_entered_at = time.time()
        self._chaossea_reschedule(self._bot_safety_s())

    def _chaossea_on_prompt(self):
        if not self._chaossea_on:
            return
        if self._chaossea_skip_ddds > 0:
            return
        self._chaossea_room_displayed()

    def _chaossea_on_room_packet(self, has_exits=True):
        if self._chaossea_on:
            self._chaossea_got_ddd = True
            if self._chaossea_skip_ddds > 0:
                self._chaossea_skip_ddds -= 1
                self._chaossea_mob_lines = []
                self._chaossea_room_mob = False
                return
            self._chaossea_room_displayed(has_exits)

    def _chaossea_room_displayed(self, has_exits=True):
        if self._chaossea_waiting_room and not self._chaossea_room_ready:
            if not has_exits:
                # The Sea of Chaos collapses every room to one vnum, and
                # its DDD packets routinely omit the exits field entirely
                # (confirmed live - logs/normal-20260623.log:357,544) -
                # self.exits may still hold a stale, unrelated room's list
                # in that case (sql_follow_ddd's moved-detection can't
                # catch it here since the vnum never changes), so don't
                # let _chaossea_pick_move act on it.
                self._chaossea_exits_unknown_retry()
                return
            self._chaossea_room_ready = True
            self._chaossea_room_mobs = len(self._chaossea_mob_lines)
            # 'out' is excluded here too - see _chaossea_pick_move - so a
            # room bordering the zone exit doesn't look like it always has
            # something left to explore.
            self._chaossea_room_exits[self._chaossea_cur] = [
                d for d in self.exits if d != "out"]
            self._chaossea_render_map()
            self._chaossea_reschedule(0)

    def _chaossea_exits_unknown_retry(self):
        """Re-poke with 'look' and wait for a fresh response instead of
        trusting self.exits, bounded so a room that never reveals exits
        stops Chaossea cleanly rather than looping forever."""
        cap = int(self.setting("chaossea_exits_retry_cap", 5))
        if self._chaossea_exits_retry >= cap:
            self._chaossea_stop(
                "can't determine this room's exits - stuck (no exits in "
                f"{cap} attempts)")
            return
        self._chaossea_exits_retry += 1
        self.write_local(
            f"[chaossea] room gave no exits (attempt "
            f"{self._chaossea_exits_retry}/{cap}) - retrying with 'look'",
            "#cc9933")
        # 'look' re-triggers the italic-marker mob/player scan for this
        # room; clear what the failed attempt already collected so a
        # successful retry doesn't double-count mob lines.
        self._chaossea_room_mob = self._chaossea_room_player = False
        self._chaossea_mob_lines = []
        self.send_line("look")
        self._chaossea_reschedule(self._bot_safety_s())

    def _chaossea_scan_markers(self, spans, clean):
        ital, under = self._line_markers(spans)
        if ital:
            self._chaossea_room_mob = True
            self._chaossea_mob_lines.append(clean)
        elif under and not self._line_is_party(clean):
            self._chaossea_room_player = True

    def _chaossea_after_kill(self):
        """A Chaossea-initiated fight just cleared (in_combat went True then
        False while self._chaossea_pending_loot was set). In fight mode,
        ignore whatever it carried entirely and move on to the next mutant.
        Otherwise loot, update the goal state, then resume the examine loop
        for any remaining mutants in this room."""
        if self._chaossea_fight_mode:
            self._chaossea_pending_loot = None
            self._chaossea_next_kill()
            return
        self.send_line("get all")          # cube isn't bind-on-pickup
        if "a chaotic charm" in self._chaossea_pending_loot:
            self.send_line("get charm")    # bind-on-pickup, get-all skips it
            self._chaossea_has_charm = True
        if "a cube of raw chaos" in self._chaossea_pending_loot:
            self._chaossea_has_cube = True
        self._chaossea_pending_loot = None
        self._chaossea_next_examine()

    def _chaossea_handle_blocked(self):
        """The last move produced no fresh DDD at all - in this zone that
        can mean either a wall or a mob in the way, and there is no
        captured wire text to tell them apart (see design doc). (Comparing
        self.room text instead of "got a DDD" was the original signal here
        and it's wrong: every room in a layer shares one generic name+vnum
        - e.g. "Layer one of the Sea of Chaos [1409]" - so a real,
        successful move into a different physical room still looks
        "unchanged" by text and used to misfire this path every time -
        confirmed live.) Since this only fires after the room's mutants
        have ALL already been examined (nothing here was confirmed to
        carry the charm/cube), it's safe to force-fight one without
        missing loot, then retry. Gives up once no mutants are left rather
        than retrying a real wall forever.
        Reads _chaossea_pre_move_mobs (snapshotted before the move sent),
        not the live _chaossea_room_mobs - the move section's
        _chaossea_enter_room() call already zeroed that for the NEXT room
        before this handler ever runs."""
        if self._chaossea_pre_move_mobs > 0:
            self._chaossea_pre_move_mobs -= 1
            self._chaossea_pending_loot = []   # already examined - confirmed
                                               # nothing needed on any of them
            self._chaossea_engage_at = time.time()
            self._chaossea_engage_whiffs = 0
            self.send_line("kill mutant")
            self._chaossea_reschedule(self._hunt_settle_s())
            return
        self._chaossea_stop(
            "can't move that way and no mutant left to blame - stuck "
            "(real wall, most likely - needs a live look)")

    def _chaossea_tick(self):
        self._chaossea_job = None
        if not self._chaossea_on:
            return
        if self.setting("chaossea_tick_debug"):
            self.write_local(
                f"[cs-tick-early] combat={self.in_combat} "
                f"dead={self.deadman_tripped} "
                f"was_fight={self._chaossea_was_fighting} "
                f"pkwait={self._chaossea_post_kill_wait} "
                f"loot={self._chaossea_pending_loot} "
                f"wr={self._chaossea_waiting_room} "
                f"rr={self._chaossea_room_ready}", "#888888")
        if not (self.conn and self.conn.alive):
            self._chaossea_stop("disconnected"); return
        if self.deadman_tripped:
            if self.in_combat:
                self._chaossea_reschedule(1.0); return
            self._chaossea_stop(
                "deadman tripped - stopped in place, no verified path home")
            return
        if self.in_combat:
            self._chaossea_was_fighting = True
            self._chaossea_engage_whiffs = 0   # the swing connected - real fight
            self._chaossea_reschedule(1.0); return
        if self._chaossea_was_fighting:
            self._chaossea_was_fighting = False
            if self._chaossea_pending_loot is not None:
                self._chaossea_post_kill_wait = True
                self._arm_post_kill_resume()   # guild vrest/revalrie hold,
                                               # if the guild config sets one
            self._chaossea_reschedule(0.3); return
        if self._chaossea_post_kill_wait:
            # the guild's own post-kill cascade (e.g. viking vrest) may still
            # be resting - every other bot engine already waits on this gate,
            # Chaossea just never had it wired in (confirmed live: it tried
            # to act mid-rest and never resumed on its own). Distinct from
            # the whiff-cap retry below: this is "the kill definitely landed,
            # just hold the loot/next-mutant step until resting clears."
            if self._post_kill_holding():
                self._chaossea_reschedule(0.3); return
            self._chaossea_post_kill_wait = False
            self._chaossea_after_kill(); return
        if self._chaossea_pending_loot is not None:
            # sent a kill, not yet in combat - same engage-grace/whiff-cap
            # pattern _bot_assess uses (3s lags a beat before the first hit
            # lands), so a whiffed keyword/ordinal doesn't hang forever
            grace = max(1.0, self.setting("hunt_engage_grace_ms", 4000) / 1000.0)
            if time.time() - self._chaossea_engage_at < grace:
                self._chaossea_reschedule(0.4); return
            self._chaossea_engage_whiffs += 1
            cap = int(self.setting("hunt_max_whiffs", 5))
            if self._chaossea_engage_whiffs >= cap:
                self._chaossea_pending_loot = None
                self._chaossea_engage_whiffs = 0
                self._chaossea_next_examine(); return
            kw = self._chaossea_mob_keyword(self._chaossea_mob_idx)
            self.send_line(f"kill {kw}")
            self._chaossea_engage_at = time.time()
            self._chaossea_reschedule(0.4); return
        if not self._chaossea_waiting_room and not self._chaossea_room_ready:
            self._chaossea_enter_room(); return
        if not self._chaossea_room_ready:
            if time.time() - self._chaossea_room_entered_at > self._bot_safety_s():
                # DDD didn't arrive in time; poke the server for a fresh display.
                # 'look' often returns a BAD packet without exits, leaving the
                # bot stuck indefinitely - the configured refresh command (glance)
                # reliably produces a DDD with exits.
                self.send_line(self.setting("bot_refresh_command", "glance"))
                self._chaossea_room_entered_at = time.time()
            self._chaossea_reschedule(0.3); return
        self._chaossea_waiting_room = False
        if self.setting("chaossea_tick_debug"):
            self.write_local(
                f"[cs-tick] pre={self._chaossea_pre_move_room} "
                f"got_ddd={self._chaossea_got_ddd} "
                f"xpend={self._chaossea_examine_pending} "
                f"done={self._chaossea_room_done} "
                f"idx={self._chaossea_mob_idx}/{self._chaossea_room_mobs} "
                f"exits={self.exits}", "#888888")
        if self._chaossea_pre_move_room is not None:
            unchanged = not self._chaossea_got_ddd
            self._chaossea_pre_move_room = None
            if unchanged:
                self._chaossea_handle_blocked(); return
        if self._chaossea_has_charm and self._chaossea_has_cube:
            self.send_line("retreat from the sea")
            self._chaossea_stop("got both - retreated"); return
        if self._chaossea_examine_pending:
            self._chaossea_reschedule(0.3); return   # waiting on an examine
        if not self._chaossea_room_done:
            if self._chaossea_mob_idx == 0 and self._chaossea_room_mobs:
                if self._chaossea_fight_mode:
                    self._chaossea_next_kill(); return
                self._chaossea_next_examine(); return
            if self._chaossea_room_mobs == 0:
                self._chaossea_room_done = True
            else:
                self._chaossea_reschedule(0.3); return
        # room fully checked (or empty) and nothing to fight here - move on
        self._chaossea_room_done = False
        path = self._chaossea_try_fast_backtrack()
        if path:
            # Dead end in fight mode: pre-advance the temp-map through every
            # step so _chaossea_cur lands at the frontier, send all directions
            # at once (server processes them in sequence at wire speed), and
            # discard the len(path)-1 intermediate DDD packets that arrive
            # before the one we actually care about.
            for d in path:
                self._chaossea_move(d)
            self._chaossea_last_dir = path[-1]
            self._chaossea_pre_move_room = self.room
            self._chaossea_got_ddd = False
            self._chaossea_pre_move_mobs = self._chaossea_room_mobs
            self._chaossea_skip_ddds = len(path) - 1
            for d in path:
                self.send_line(d)
            self._chaossea_enter_room()
            return
        d = self._chaossea_pick_move(self.exits)
        if d is None:
            self._chaossea_stop("no exit from here"); return
        self._chaossea_move(d)
        self._chaossea_last_dir = d
        self._chaossea_pre_move_room = self.room
        self._chaossea_got_ddd = False
        self._chaossea_pre_move_mobs = self._chaossea_room_mobs
        self.send_line(d)
        self._chaossea_enter_room()

    # ------------------------------------------------ Chaossea temp-map
    # A session-local graph for zones that regenerate their layout per visit
    # and collapse every room to one vnum (Sea of Chaos): fake room ids
    # (f00001, f00002, ...) connected by DIRECTIONS TAKEN, no roomid
    # verification at all. Moving through a direction not yet recorded from
    # the current fake room allocates a new one and records BOTH the
    # forward edge and the assumed reciprocal (opposite-direction) edge back
    # - an assumption, not a proof, but the only way to recognize
    # backtracking instead of the map growing without bound. See the design
    # doc (docs/superpowers/specs/2026-06-20-mapless-wander-and-chaossea-
    # design.md) for why this is a reasonable bet for this specific zone.
    def _chaossea_alloc_room(self):
        self._chaossea_next_id += 1
        return f"f{self._chaossea_next_id:05d}"

    def _chaossea_move(self, direction):
        """Record (or reuse) the edge for `direction` from the current fake
        room, advance _chaossea_cur to the result, and return it."""
        cur = self._chaossea_cur
        edges = self._chaossea_map.setdefault(cur, {})
        target = edges.get(direction)
        if target is None:
            self._chaossea_next_id += 1
            target = f"f{self._chaossea_next_id:05d}"
            edges[direction] = target
            opp = _DIR_OPPOSITE.get(direction)
            if opp:
                self._chaossea_map.setdefault(target, {})[opp] = cur
        self._chaossea_cur = target
        return target

    def _chaossea_frontier_step(self):
        """BFS over the temp map for the nearest OTHER room that still has
        a live exit not yet recorded as an edge from it (per the
        _chaossea_room_exits snapshots), returning the first direction to
        step toward it - real retracing once the local pocket is
        exhausted, instead of blind wandering. Returns None if no such
        room is reachable (e.g. nothing visited has unexplored exits, or
        a just-allocated room we've never stood in yet)."""
        start = self._chaossea_cur
        seen = {start}
        queue = deque([(start, [])])
        while queue:
            room, path = queue.popleft()
            if path:
                room_exits = self._chaossea_room_exits.get(room)
                known = self._chaossea_map.get(room, {})
                if room_exits and any(e not in known for e in room_exits):
                    return path[0]
            for d, nxt in self._chaossea_map.get(room, {}).items():
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, path + [d]))
        return None

    def _chaossea_frontier_path(self):
        """Like _chaossea_frontier_step but returns the full BFS path list
        to the nearest room with unexplored exits, not just the first step."""
        start = self._chaossea_cur
        seen = {start}
        queue = deque([(start, [])])
        while queue:
            room, path = queue.popleft()
            if path:
                room_exits = self._chaossea_room_exits.get(room)
                known = self._chaossea_map.get(room, {})
                if room_exits and any(e not in known for e in room_exits):
                    return path
            for d, nxt in self._chaossea_map.get(room, {}).items():
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, path + [d]))
        return None

    def _chaossea_try_fast_backtrack(self):
        """In fight mode only: if the current room is a dead end (all exits
        already mapped, no 'd'), return the full BFS path to the nearest
        frontier so the caller can speedwalk there in one burst. Returns None
        to fall through to normal single-step pick_move."""
        if not self._chaossea_fight_mode:
            return None
        exits_no_out = [d for d in (self.exits or []) if d != "out"]
        if "d" in exits_no_out:
            return None
        known = self._chaossea_map.get(self._chaossea_cur, {})
        if any(d not in known for d in exits_no_out):
            return None
        path = self._chaossea_frontier_path()
        if not path or len(path) <= 1:
            return None
        return path

    def _chaossea_pick_move(self, exits):
        """Movement priority: always dive a 'down' exit the moment it's
        available (the Sea has multiple floors and diving is the goal);
        else prefer a direction not yet recorded from the current fake
        room (drives systematic floor coverage); else BFS the temp map for
        the nearest room with a still-unexplored exit and step toward it;
        else fall back to a known direction, excluding the immediate
        reverse of the last move unless it's the only option. Returns None
        if `exits` is empty (a real dead end).

        'out' is excluded from every candidate list: it's the zone-exit
        command (one 'out' leaves the maze for the lobby, a second leaves
        the lobby for the regular game world entirely), not a maze-internal
        connector - confirmed live when the novelty-preferring pick walked
        the character straight out of the Sea through it. Retreating on
        purpose (item-hunt's goal-complete case) sends it directly, never
        through this picker."""
        exits = [d for d in (exits or []) if d != "out"]
        if not exits:
            return None
        if "d" in exits:
            return "d"
        known = self._chaossea_map.get(self._chaossea_cur, {})
        unexplored = [d for d in exits if d not in known]
        if unexplored:
            return random.choice(unexplored)
        step = self._chaossea_frontier_step()
        if step:
            return step
        reverse = _DIR_OPPOSITE.get(self._chaossea_last_dir)
        forward = [d for d in exits if d != reverse]
        return random.choice(forward or exits)

    def _chaossea_render_map(self):
        """Embed the session-local fake-room graph into (x,y,z) and draw
        the current floor's slice in the map pane - same payload shape and
        renderer (MapPane.set_graph) as the real charted map, so "where
        have I been" survives a Chaossea break with no new UI. z tracks
        floors (a 'd'/'u' edge changes z, not x/y) so two unrelated floors
        that happen to reuse the same (x,y) never falsely collide; (x,y)
        alone moves via MapPane.DIRS, same as every other direction on the
        real map. Reverting to the normal map needs no special-case
        cleanup: the next real (non-Chaossea) move calls the normal
        sqlite-map render, which overwrites this pane's graph as usual."""
        start = "f00001"
        coords = {start: (0, 0, 0)}
        seen = {start}
        queue = deque([start])
        occupied = {(0, 0, 0): start}
        collide = set()
        while queue:
            room = queue.popleft()
            x, y, z = coords[room]
            for d, nxt in self._chaossea_map.get(room, {}).items():
                if nxt in seen:
                    continue
                seen.add(nxt)
                off = MapPane.DIRS.get(d)
                if off:
                    coord = (x + off[0], y + off[1], z)
                elif d in ("d", "down"):
                    coord = (x, y, z + 1)
                elif d in ("u", "up"):
                    coord = (x, y, z - 1)
                else:
                    coord = (x, y, z)
                coords[nxt] = coord
                if coord in occupied:
                    collide.add(coord)
                else:
                    occupied[coord] = nxt
                queue.append(nxt)
        cur_z = coords.get(self._chaossea_cur, (0, 0, 0))[2]
        payload = {}
        for room, (x, y, z) in coords.items():
            if z != cur_z:
                continue
            known = self._chaossea_map.get(room, {})
            exits = self._chaossea_room_exits.get(room, [])
            payload[(x, y)] = {
                "rid": room, "exits": exits,
                "stubs": [d for d in exits if d not in known],
                "here": room == self._chaossea_cur,
                "collide": (x, y, z) in collide,
                "house": False, "new": False,
            }
        label = "fight" if self._chaossea_fight_mode else "item-hunt"
        self.map.set_graph(
            payload, area="Sea of Chaos", coder="",
            status=f"chaossea {label} - {len(coords)} rooms, floor {cur_z}",
            mapping=False)

    # ------------------------------------------------ path bot (Run)
    # Imported tintin steppers (tools/convert_tin_bots.py -> muds/<mud>/
    # bots/*.json): walk a fixed route; in each room, if a line matches the
    # bot's target-mob whitelist, `kill <target>` and wait out combat, then
    # re-check the room; at the end, loop or stop. No map needed - the route
    # is explicit - so unlike Bot/Hunt this runs on the TinMap (3k) muds.
    # Reimplements generic.tin's core loop, not its guild-specific cruft.
    def _run_bots_dir(self):
        return os.path.join(paths.mud_dir(self.mud), "bots")

    def _run_available(self):
        d = self._run_bots_dir()
        if not os.path.isdir(d):
            return []
        return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))

    def cmd_run(self, arg):
        bits = arg.split()
        if not bits:
            if self._run_precheck:
                self.write_local(
                    f"[run] '{self._run_name}' - checking precondition. "
                    "Run off to stop.", "#cc9933")
            elif self._run_on:
                self.write_local(
                    f"[run] '{self._run_name}' - step {self._run_idx}/"
                    f"{len(self._run_path)}, {self._run_kills} kill(s). "
                    "Run off to stop.", "#cc9933")
            else:
                avail = ", ".join(self._run_available()) or "(none)"
                self.write_local(
                    f"Run <bot> [loop] [<step>] | Run off. Available: "
                    f"{avail}", "#cc9933")
            return
        a0 = bits[0].lower()
        if a0 in ("off", "stop", "halt"):
            if self._run_on:
                self._run_stop("stopped")
            else:
                self.write_local("Run is not active.", "#cc9933")
            return
        if self._run_on:
            self.write_local(f"[run] '{self._run_name}' already running. "
                             "Run off first.", "#cc9933")
            return
        loop = False
        start_idx = 0
        for b in bits[1:]:
            bl = b.lower()
            if bl in ("loop", "l"):
                loop = True
            elif bl.isdigit():
                start_idx = int(bl)
        self._run_start(a0, loop, start_idx)

    def _run_start(self, name, loop, start_idx=0):
        path = os.path.join(self._run_bots_dir(), f"{name}.json")
        if not os.path.exists(path):
            avail = ", ".join(self._run_available()) or "(none)"
            self.write_local(f"No bot '{name}'. Available: {avail}",
                             "#cc6666")
            return
        data, err = paths.load_json(path, default=None)
        if err or not data:
            self.write_local(f"[run] couldn't load '{name}': "
                             f"{err or 'empty'}", "#cc6666")
            return
        if data.get("_incomplete"):
            self.write_local(f"[run] '{name}' carries trigger logic not yet "
                             "ported (not a Tier-1 bot) - skipping.",
                             "#cc6666")
            return
        steps = [s for s in (data.get("path") or []) if s]
        if not steps:
            self.write_local(f"[run] '{name}' has no path.", "#cc6666")
            return
        if start_idx < 0 or start_idx > len(steps):
            self.write_local(
                f"[run] '{name}' has {len(steps)} steps - can't resume at "
                f"{start_idx}.", "#cc6666")
            return
        self._run_started = time.time()
        self._run_kills = 0
        self._run_name = name
        self._run_path = steps
        self._run_idx = start_idx
        precheck = data.get("precheck")
        if precheck and precheck.get("cmd") and precheck.get("fail_contains"):
            self._run_on = True             # claims the Run slot (Esc/#stop work)
            self._run_precheck = {
                "name": name, "loop": loop, "data": data, "steps": steps,
                "start_idx": start_idx,
                "fail_contains": precheck["fail_contains"].strip().lower(),
                "message": precheck.get("message")
                           or f"precheck '{precheck['cmd']}' failed",
            }
            self.write_local(
                f"[run] '{name}': checking precondition (\"{precheck['cmd']}\")"
                "...", "#cc9933")
            self.send_line(precheck["cmd"])
            self._run_precheck_job = self.root.after(
                int(precheck.get("settle_ms", 1500)), self._run_precheck_timeout)
            return
        self._run_start_finish(name, loop, data, steps, start_idx)

    def _run_precheck_scan_line(self, clean):
        """Watch the response to a pending bot precheck command (e.g. 'exa
        axe' before a treehouse bot, which needs the axe at the end for the
        reward). A line containing fail_contains aborts the launch instead
        of starting the walk; otherwise _run_precheck_timeout lets it
        through once the settle window passes with no match."""
        pc = self._run_precheck
        if not pc or pc["fail_contains"] not in clean.lower():
            return
        if self._run_precheck_job:
            self.root.after_cancel(self._run_precheck_job)
            self._run_precheck_job = None
        name, msg = pc["name"], pc["message"]
        self._run_precheck = None
        self._run_on = False
        self.write_local(f"[run] '{name}' not started - {msg}", "#cc6666")

    def _run_precheck_timeout(self):
        self._run_precheck_job = None
        pc = self._run_precheck
        if not pc:
            return
        self._run_precheck = None
        self._run_start_finish(pc["name"], pc["loop"], pc["data"], pc["steps"],
                               pc.get("start_idx", 0))

    def _run_start_finish(self, name, loop, data, steps, start_idx=0):
        self._run_mobs = [(m["match"].strip().lower(), m["target"].strip(),
                           bool(m.get("contains")))
                          for m in (data.get("mobs") or [])
                          if m.get("match") and m.get("target")]
        self._run_setup = list(data.get("setup") or [])
        ak = data.get("after_kill") or []
        self._run_after_kill = [ak] if isinstance(ak, str) else list(ak)
        self._run_skip = [str(s).strip().lower()
                          for s in (data.get("skip_phrases") or [])
                          if str(s).strip()]
        self._run_room_player = False
        self._resume_pending = False
        self._run_path = steps
        self._run_idx = start_idx
        self._run_loop = loop or bool(data.get("loop"))
        self._run_name = name
        self._run_lines = []
        self._run_room_enter_t = time.time()
        self._run_acted = False
        self._run_killing = False
        self._run_kill_start = 0.0
        self._run_engaged = False
        self._run_combat_end = 0.0
        self._run_kills = 0
        self._run_started = time.time()
        self._run_on = True
        area = data.get("area", name)
        resuming = f", resuming at step {start_idx}" if start_idx else ""
        self.write_local(
            f"[run] '{name}' ({area}): {len(steps)} steps, "
            f"{len(self._run_mobs)} target mobs"
            + (", looping" if self._run_loop else "") + resuming
            + ". Run off to stop. (Needs autocombat ON; stops on deadman.)",
            "#66cc66")
        if not start_idx:           # entrance setup only applies fresh from
            for c in self._run_setup:   # the start room - skip it on resume
                self.send_line(c)
        self.send_line("pwho")              # seed party whitelist for skipping
        self._bot_pwho_at = time.time()
        self._run_reschedule(0.6)

    def _run_stop(self, why=""):
        self._run_on = False
        if self._run_job:
            self.root.after_cancel(self._run_job)
            self._run_job = None
        if self._run_precheck_job:
            self.root.after_cancel(self._run_precheck_job)
            self._run_precheck_job = None
        self._run_precheck = None
        secs = max(1, int(time.time() - self._run_started))
        self.write_local(f"[run] {why or 'off'} ({self._run_kills} kills in "
                         f"{secs}s).", "#aa88cc")

    def _run_reschedule(self, secs):
        if self._run_job:
            self.root.after_cancel(self._run_job)
        self._run_job = self.root.after(int(secs * 1000), self._run_tick)

    def _run_move_s(self):
        return max(0.3, self.setting("run_move_ms", 1300) / 1000.0)

    def _run_settle_s(self):
        return max(0.5, self.setting("run_settle_ms", 1500) / 1000.0)

    def _run_engage_grace_s(self):
        # How long to wait for combat to actually start after sending the kill
        # before giving up on the mob. 3s lags a beat before the first hit lands
        # (same gotcha the Hunt bot guards against with hunt_engage_grace_ms) -
        # the short settle alone made Run roam off a live mob "after 1 round".
        return max(self._run_settle_s(),
                   self.setting("run_engage_grace_ms", 4000) / 1000.0)

    def _run_postcombat_s(self):
        return max(0.0, self.setting("run_postcombat_ms", 2000) / 1000.0)

    def _run_room_attackers(self):
        """Names of mobs whose buffered room line already shows them mid-
        attack, e.g. 'Flesh Golem attacking you!.' or 'Steel Golem
        [scratched] attacking you!.' - the mud's auto-combat already has
        these on you whether or not they're on the kill list, and
        _run_scan_mob's exact-name match never catches them (the suffix
        breaks it). Used to hold movement instead of reading the room as
        clear while self.in_combat hasn't caught up yet."""
        names = []
        for raw in self._run_lines:
            m = RUN_ATTACKING_RE.match(raw.strip())
            if m:
                names.append(m.group(1).strip())
        return names

    def _run_scan_mob(self):
        """Scan the room text since the last step for a whitelisted mob. The
        3k room shows each mob on its own line as '<Long Name>.' (see the
        capture), so by default match a line == a botmob's display name; a mob
        entry with "contains": true matches the keyword anywhere in the line
        (the looser keyword style the script bot used). Returns
        (target_keyword, display_name) or (None, None)."""
        for raw in self._run_lines:
            s = raw.strip()
            if not s:
                continue
            key = s[:-1].strip() if s.endswith(".") else s
            key = RUN_COUNT_TAG_RE.sub("", key).strip()  # drop "{2}" etc.
            kl = key.lower()
            for match_l, target, contains in self._run_mobs:
                if (match_l in kl) if contains else (kl == match_l):
                    return target, key
        return None, None

    def _run_scan_markers(self, spans, clean):
        """One room line for Run: buffer it for the mob scan, and flag a
        non-party player (look_player underline marker) so Run cedes the room
        - the same skip behaviour Bot/Hunt have. On a MUD without the asets
        (e.g. 3k) there's no marker, so skip_phrases is the cross-mud fallback."""
        self._run_lines.append(clean)
        self._run_lines = self._run_lines[-60:]
        _ital, under = self._line_markers(spans)
        if under and not self._line_is_party(clean):
            self._run_room_player = True

    def _run_room_blocked(self):
        """Reason to skip this room (non-party player, or a skip_phrase like a
        guild disguise), or None to hunt it normally."""
        if self._run_room_player:
            return "player"
        for raw in self._run_lines:
            low = raw.lower()
            for ph in self._run_skip:
                if ph in low:
                    return f"'{ph}'"
        return None

    def _run_send_step(self, step):
        for cmd in step.split(";"):
            cmd = cmd.strip()
            if cmd:
                self.send_line(cmd)

    def _run_tick(self):
        self._run_job = None
        if not self._run_on:
            return
        if not (self.conn and self.conn.alive):
            self._run_stop("disconnected"); return
        # deadman: the bot never keeps it alive - finish any fight, then stop.
        if self.deadman_tripped:
            if self.in_combat:
                self._run_reschedule(1.0); return
            self._run_stop("deadman tripped"); return
        # Fight resolution is driven SOLELY by in_combat (the FFF K=enemy
        # field): True while a mob is engaged, False the instant it dies/flees.
        # Verified reliable on 3k. We deliberately do NOT gate on the kill line
        # (`dealt the killing blow`) - a necro's undead pet dying produces that
        # line too, which was making the bot leave mid-fight. No time caps: a
        # fight ends when K clears, however long that takes.
        if self.in_combat and not self._run_killing:    # engaged (our kill/aggro)
            self._run_killing = True
            self._run_engaged = True
            self._run_kill_start = time.time()
        if self._run_killing:
            if self.in_combat:
                self._run_engaged = True
                self._run_reschedule(1.0); return        # fight in progress
            if self._run_engaged:                        # enemy cleared -> over
                self._run_killing = False
                self._run_engaged = False
                self._run_combat_end = time.time()
                self._run_acted = False                  # re-check room for more
                self._run_lines = []                     # drop the dead mob line
                self._run_room_enter_t = time.time()
                self._run_room_player = False
                for c in self._run_after_kill:           # loot, e.g. 'get plate'
                    self.send_line(c)
                self._arm_post_kill_resume()             # guild revalrie hold
                self.send_line(self.setting("run_refresh_command", "look"))
                self._run_reschedule(self._run_postcombat_s()); return
            # Sent the kill but combat hasn't registered yet. 3s lags a beat
            # before the first hit lands (the Hunt bot's hunt_engage_grace_ms
            # gotcha) - don't roam off a live mob after the short settle. While
            # the target is still in the room AND we're inside the engage grace,
            # keep waiting, re-issuing the kill in case the first whiffed on a
            # keyword/lag; autocombat finishes it once engaged. Only give up
            # once the grace fully expires (mob gone, or keyword never took).
            if time.time() - self._run_kill_start < self._run_engage_grace_s():
                target, _name = self._run_scan_mob()
                if target:
                    self.send_line(f"kill {target}")     # re-poke; cheap if dead
                self._run_reschedule(self._run_settle_s()); return
            self._run_killing = False                    # whiffed - move on
        if time.time() - self._run_combat_end < self._run_postcombat_s() \
                or self._post_kill_holding():
            self._run_reschedule(0.4); return            # loot/harvest/revalrie
        # cede a room with a non-party player (or a skip_phrase), else clear
        # its whitelisted mobs before advancing
        if not self._run_acted:
            if self._run_room_attackers() and \
                    time.time() - self._run_room_enter_t < self._run_engage_grace_s():
                # The room text already shows a mob mid-attack, but
                # self.in_combat (FFF K/enemy) hasn't caught up yet - hold
                # movement instead of reading the room as clear.
                self._run_reschedule(self._run_settle_s()); return
            self._run_acted = True
            blocked = self._run_room_blocked()
            if blocked:
                self.write_local(f"[run] {blocked} in room - skipping.",
                                 "#cc9933")
            else:
                target, name = self._run_scan_mob()
                if target:
                    self.write_local(f"[run] {name} -> kill {target}",
                                     "#88cc88")
                    self._run_killing = True
                    self._run_engaged = False
                    self._run_kill_start = time.time()
                    self.send_line(f"kill {target}")
                    self._run_reschedule(self._run_settle_s()); return
        # advance along the route
        if self._run_idx >= len(self._run_path):
            if self._run_loop:
                self._run_idx = 0
                self._run_lines = []
                self._run_room_enter_t = time.time()
                self._run_acted = False
                self._run_room_player = False
                self.write_local("[run] loop complete - restarting route.",
                                 "#88cc88")
                self._run_reschedule(self._run_move_s()); return
            self._run_stop("route complete"); return
        step = self._run_path[self._run_idx]
        self._run_idx += 1
        self._run_lines = []
        self._run_room_enter_t = time.time()
        self._run_acted = False
        self._run_room_player = False
        self._run_send_step(step)
        self._run_reschedule(self._run_move_s())

    # -------------------------------------------- guild macros (gated)
    # A guild's cascade "macros" section names a gated step sequence -
    # send, wait for text, send the next - run with its capitalized name
    # (e.g. "Autoregen sword" for the bladesinger rune chain in
    # muds/3k/guilds/bladesingers.json). New running aliases are pure
    # data: add an entry to "macros", no code change needed.
    def _macro_defs(self):
        return self.cascade.get("macros", {}) or {}

    def cmd_macro(self, name, arg):
        a0 = arg.strip().split()[0].lower() if arg.strip() else ""
        if a0 in ("off", "stop", "halt"):
            if self._macro_on and self._macro_name == name:
                self._macro_stop("stopped")
            elif self._macro_on:
                self.write_local(
                    f"[macro] '{self._macro_name}' is running, not '{name}'.",
                    "#cc9933")
            else:
                self.write_local(f"[macro] '{name}' is not active.",
                                 "#cc9933")
            return
        if self._macro_on:
            self.write_local(
                f"[macro] '{self._macro_name}' already running. "
                f"{self._macro_name.title()} off to stop.", "#cc9933")
            return
        steps = self._macro_defs().get(name)
        if not steps:
            self.write_local(f"[macro] '{name}' has no steps.", "#cc6666")
            return
        item = arg.strip()
        if not item:
            self.write_local(f"Usage: {name.title()} <item>", "#cc9933")
            return
        self._macro_steps = [
            (str(s.get("cmd", "")).replace("{x}", item),
             str(s["wait"]).strip().lower() if s.get("wait") else None)
            for s in steps]
        self._macro_name = name
        self._macro_idx = 0
        self._macro_on = True
        self.write_local(
            f"[macro] '{name}' on '{item}': {len(self._macro_steps)} "
            f"step(s). {name.title()} off to stop.", "#66cc66")
        self._macro_advance()

    def _macro_advance(self):
        if not self._macro_on:
            return
        if self._macro_idx >= len(self._macro_steps):
            self._macro_stop("done")
            return
        cmd, wait = self._macro_steps[self._macro_idx]
        self.send_line(cmd)
        if wait is None:
            self._macro_idx += 1
            self._macro_advance()
            return
        if self._macro_job:
            self.root.after_cancel(self._macro_job)
        self._macro_job = self.root.after(
            int(self.setting("macro_step_timeout_ms", 20000)),
            self._macro_timeout)

    def _macro_timeout(self):
        self._macro_job = None
        if not self._macro_on:
            return
        _cmd, wait = self._macro_steps[self._macro_idx]
        self._macro_stop(
            f"no \"{wait}\" seen within the timeout - stopped at step "
            f"{self._macro_idx + 1}/{len(self._macro_steps)}")

    def _macro_scan_line(self, clean):
        if not self._macro_on or self._macro_idx >= len(self._macro_steps):
            return
        _cmd, wait = self._macro_steps[self._macro_idx]
        if wait is None or wait not in clean.lower():
            return
        if self._macro_job:
            self.root.after_cancel(self._macro_job)
            self._macro_job = None
        self._macro_idx += 1
        self._macro_advance()

    def _macro_stop(self, why=""):
        self._macro_on = False
        if self._macro_job:
            self.root.after_cancel(self._macro_job)
            self._macro_job = None
        self.write_local(f"[macro] '{self._macro_name}' {why or 'off'}.",
                         "#aa88cc")

    # ------------------------------------------------ explore (auto-chart)
    # Walk an area's open stubs and chart the rooms beyond, so you don't have
    # to do it by hand. Scopes to the current room's area; pathfinds to the
    # nearest unprobed stub over charted edges, then sends the stub's move -
    # all THROUGH the charting gate, which lock-steps + rates each arrival.
    # A stub that doesn't chart (locked door, one-way, no BAD) is remembered
    # as tried so it isn't retried; newly-charted rooms add their own stubs to
    # the frontier, so the area fills outward until nothing reachable is left.
    # ---- chartwalk: `Chart <entry> [return] [simple]` -----------------------
    # One-shot initial charter: from the room you stand in, take an entry
    # command into an unknown area, chart every room + complete every stub
    # (bounded to the entry room's rated area_id, so it stops at foreign-area
    # rooms AND the start room), then return to start and report. Reuses the
    # Explore engine: bootstrap to learn the area_id, then `_explore_tick`.
    COMPASS_DIRS = frozenset(
        ("n", "s", "e", "w", "ne", "nw", "se", "sw", "u", "d"))
    # Explore only auto-probes the eight horizontal directions (cardinal +
    # ordinal). Up/down/enter/out/in/special exits break the 2D map and are
    # left for manual review (see _explore_flag_review), not auto-walked.
    CARDINAL_DIRS = frozenset(("n", "s", "e", "w", "ne", "nw", "se", "sw"))
    _REVERSE_DIR = {"n": "s", "s": "n", "e": "w", "w": "e",
                    "ne": "sw", "sw": "ne", "nw": "se", "se": "nw",
                    "u": "d", "d": "u"}
    # Explore keeps charting ON (gated, position confirmed by each room packet)
    # for short hops between stubs - so it can never chart from a stale room.
    # Only a jump of MORE than this many rooms drops charting for a fast walk,
    # which by then is across a mostly-mapped, reliable region.
    _EXPLORE_FAR_HOPS = 2

    def cmd_chartwalk(self, arg):
        toks = arg.split()
        if toks and toks[0].lower() in ("off", "stop", "halt"):
            if self._explore_on:
                self._explore_stop("stopped")
            else:
                self.write_local("Chart walk is not running.", "#cc9933")
            return
        if self._explore_on:
            self.write_local("[chart] already running. Chart off / #stop "
                             "to stop.", "#cc9933")
            return
        simple = any(t.lower() == "simple" for t in toks)
        toks = [t for t in toks if t.lower() != "simple"]
        if not toks:
            self.write_local("Usage: Chart <entry-cmd> [return-cmd] [simple]",
                             "#cc9933")
            return
        entry = toks[0]
        return_cmd = toks[1] if len(toks) > 1 else None
        self._chartwalk_start(entry, return_cmd, simple)

    def _chartwalk_start(self, entry, return_cmd, simple):
        if self.map_backend != "sqlite" or self.mapdb is None:
            self.write_local("[chart] needs the sqlite map backend.",
                             "#cc6666")
            return
        s = self._sql_cur_vnum
        if s is None:
            self.write_local("[chart] location unknown - move a room first.",
                             "#cc6666")
            return
        self._explore_on = True
        self._explore_phase = "bootstrap"
        self._explore_return_to = s
        self._explore_simple = simple
        self._explore_entry = entry
        self._explore_return_cmd = return_cmd
        self._explore_area_id = None
        self._explore_area_name = "?"
        self._explore_tried = set()
        self._explore_recharted = set()
        self._explore_flagged = set()
        self._explore_rated_tried = set()
        self._explore_rating = None
        self._explore_rate_back = None
        self._explore_plan = []
        self._explore_target = None
        self._explore_travel = False
        self._explore_travel_dest = None
        self._explore_travel_wait = 0
        self._explore_start_n = 0
        self._explore_entered_chart = not self._sql_charting
        if self._explore_entered_chart:
            self.sql_set_chart_mode(True)
        self.write_local(
            f"[chart] entering via '{entry}' from #{s} to auto-chart the area"
            ". Charts every cardinal/ordinal stub (up/down/enter/out flagged "
            "for review), then returns. Chart off / #stop to stop. (Pauses in "
            "combat, stops on deadman; supervise it.)",
            "#66cc66")
        self.send_line(entry)                # gated -> charts the first room
        self._explore_reschedule(0.6)        # tick waits for the gate to settle

    def _chartwalk_bootstrap_done(self):
        """The entry move has settled; the first area room should be charted.
        Learn its area_id and hand off to the Explore loop."""
        v = self._sql_cur_vnum
        if v == self._explore_return_to or v is None \
                or not self.mapdb.has_room(v):
            self._explore_stop(f"entry '{self._explore_entry}' didn't reach a "
                               "new charted room"); return
        area_id = self._bot_area_of(v)
        if area_id is None:
            self._explore_stop("entry room has no area (rating failed?) - "
                               "can't scope the chart"); return
        self._explore_area_id = area_id
        self._explore_area_name = self._area_name(area_id)
        self._explore_start_n = self._explore_stub_count()
        self._explore_phase = "explore"
        self.write_local(
            f"[chart] in '{self._explore_area_name}' (#{v}) - charting outward.",
            "#66cc66")
        self._explore_audit_fix()       # correct any mischarts up front
        self._explore_flag_review()     # surface non-cardinal stubs up front
        self._explore_reschedule(0.2)

    def _chartwalk_begin_return(self):
        """Stubs exhausted - build the walk back to the start room."""
        self._explore_phase = "return"
        cur, s = self._sql_cur_vnum, self._explore_return_to
        plan = None
        edges = self.mapdb.find_path(cur, s) if cur != s else []
        if edges is not None:
            plan = [c for e in edges for c in self._edge_cmds(e)]
        elif self._explore_return_cmd:
            # asymmetric entry (no charted edge home): walk back to the entry
            # room, then the given return command (e.g. 'out') hops to start.
            entry_room = self._chartwalk_entry_room()
            back = (self.mapdb.find_path(cur, entry_room)
                    if entry_room is not None else None)
            if back is not None:
                plan = [c for e in back for c in self._edge_cmds(e)]
                plan.append(self._explore_return_cmd)
        if plan is None:
            self._chartwalk_report("charted, but couldn't auto-return "
                                   f"(stranded at #{cur})")
            self._explore_stop("done (no path home)")
            return
        # Nothing left to chart - walk home fast with charting OFF (the return
        # phase is exempt from the 'charting turned off' stop in the tick).
        if self._sql_charting:
            self.sql_set_chart_mode(False, announce=False)
        self._explore_plan = plan
        self.write_local(
            f"[explore] done - walking back to the entrance #{s} "
            f"({len(plan)} hop(s))", "#66cc66")
        self._explore_reschedule(0.2)

    def _chartwalk_entry_room(self):
        """The first room charted past the start (dest of the entry edge)."""
        for e in self.mapdb.iter_exits(self._explore_return_to):
            if e["command"] == self._explore_entry \
                    or e["direction"] == self._explore_entry:
                return e["to_vnum"]
        return None

    def _chartwalk_report(self, why):
        n = self.mapdb.conn.execute(
            "SELECT COUNT(*) c FROM rooms WHERE area_id=?",
            (self._explore_area_id,)).fetchone()["c"] \
            if self._explore_area_id is not None else 0
        left = self._explore_stub_count()
        self.write_local(
            f"[chart] {why} - '{self._explore_area_name}': {n} rooms charted, "
            f"{left} stub(s) still open.", "#66cc66")

    def cmd_mapwipe(self, arg):
        """`Mapwipe confirm` - DELETE every room + exit of the current room's
        area so it can be re-charted from scratch (the area row/name is kept;
        inbound doors from other areas survive as stubs). Destructive, so bare
        `Mapwipe` only previews. Stand anywhere in the area to wipe."""
        if self.map_backend != "sqlite" or self.mapdb is None:
            self.write_local("[mapwipe] needs the sqlite map backend.",
                             "#cc6666")
            return
        if self._explore_on:
            self.write_local("[mapwipe] stop Explore first (Explore off).",
                             "#cc9933")
            return
        v = self._sql_cur_vnum
        area_id = self._bot_area_of(v) if v is not None else None
        if area_id is None:
            self.write_local("[mapwipe] this room has no charted area - stand "
                             "in a rated room of the area to wipe.", "#cc6666")
            return
        name = self._area_name(area_id)
        n = self.mapdb.conn.execute(
            "SELECT COUNT(*) c FROM rooms WHERE area_id=?",
            (area_id,)).fetchone()["c"]
        if arg.strip().lower() != "confirm":
            self.write_local(
                f"[mapwipe] would DELETE all {n} room(s) + exits of "
                f"'{name}' (#{area_id}) for a clean re-chart. Inbound doors from "
                "other areas become stubs. Type 'Mapwipe confirm' to do it.",
                "#cc9933")
            return
        rd, inb, lmd = self.mapdb.wipe_area(area_id)
        self.write_local(
            f"[mapwipe] wiped '{name}': {rd} room(s) deleted, {inb} inbound "
            f"exit(s) re-stubbed, {lmd} landmark(s) removed. Walk through it in "
            "Chart mode (or run Explore from a charted neighbour) to re-chart.",
            "#66cc66")
        self.sql_set_status()

    def cmd_mapcheck(self, arg):
        """`Mapcheck` - read-only audit of the current room's area. Lists the
        inconsistencies Explore looks for (reverse contradictions, same-
        direction collisions, dupe-target exits, unrated in-area rooms, and
        non-cardinal stubs) WITHOUT changing the map. Identity is room-id +
        area + exits only; short descs are never consulted."""
        if self.map_backend != "sqlite" or self.mapdb is None:
            self.write_local("[mapcheck] needs the sqlite map backend.",
                             "#cc6666")
            return
        v = self._sql_cur_vnum
        area_id = self._bot_area_of(v) if v is not None else None
        if area_id is None:
            self.write_local("[mapcheck] this room has no charted area - stand "
                             "in a rated room first.", "#cc6666")
            return
        name = self._area_name(area_id)
        issues = 0
        self.write_local(f"[mapcheck] auditing '{name}' (#{area_id})...",
                         "#88aacc")

        # same-direction collisions (with adjudication verdict)
        for d, t, froms in self.mapdb.same_dir_collisions(area_id):
            issues += 1
            rd = REVERSE_DIR.get(d)
            rev = self.mapdb.get_exit(t, rd) if rd else None
            winner = rev["to_vnum"] if rev else None
            if d in self.CARDINAL_DIRS and winner in froms:
                losers = [f for f in froms if f != winner]
                verdict = (f"#{t} {rd}->#{winner} confirms #{winner}; "
                           f"re-chart {losers}")
            else:
                verdict = "ambiguous (reverse exit doesn't resolve it)"
            self.write_local(
                f"  COLLISION {d}->#{t} from {sorted(froms)} - {verdict}",
                "#ff6666")

        # dupe-target exits (same room, 2+ dirs -> one room)
        for fv, tv, rows in self.mapdb.dupe_target_exits(area_id):
            issues += 1
            dirs = ",".join(r["direction"] for r in rows)
            self.write_local(f"  DUPE #{fv}: {dirs} all -> #{tv}", "#ff6666")

        # reverse contradictions (informational; collisions above name the fix)
        rc = self.mapdb.reverse_contradictions(area_id)
        for a, d, b, c in rc:
            issues += 1
            self.write_local(
                f"  REVERSE #{a} {d}->#{b}, but #{b} {REVERSE_DIR.get(d)}->#{c}",
                "#ffaa44")

        # unrated in-area frontier rooms
        unrated = sorted({r["to_vnum"]
                          for r in self.mapdb.unrated_frontier(area_id)})
        if unrated:
            issues += len(unrated)
            self.write_local(
                f"  UNRATED rooms linked from the area: {unrated} "
                "(Explore walks to + rates these)", "#ffaa44")

        # non-cardinal open stubs (manual review)
        nc = sorted(f"#{r['from_vnum']} {r['direction']}"
                    for r in self.mapdb.stubs_in_area(area_id)
                    if r["direction"] not in self.CARDINAL_DIRS)
        if nc:
            issues += len(nc)
            self.write_local(f"  NON-CARDINAL stubs (manual): {', '.join(nc)}",
                             "#ffaa44")

        self.write_local(
            f"[mapcheck] '{name}': {issues} issue(s) found."
            if issues else f"[mapcheck] '{name}': clean.",
            "#66cc66" if not issues else "#cc9933")

    def cmd_explore(self, arg):
        a = arg.strip().lower()
        if a in ("off", "stop", "halt"):
            if self._explore_on:
                self._explore_stop("stopped")
            else:
                self.write_local("Explore is not running.", "#cc9933")
            return
        if self._explore_on:
            n = self._explore_stub_count()
            self.write_local(
                f"[explore] running '{self._explore_area_name}' - "
                f"{n} stub(s) left. Explore off to stop.", "#cc9933")
            return
        self._explore_start()

    def _explore_stub_count(self):
        """Open stubs Explore will actually auto-probe: cardinal/ordinal only,
        not yet tried. Non-directional stubs are reported separately, so they
        don't keep this count (and the 'complete' check) above zero."""
        if self.mapdb is None or self._explore_area_id is None:
            return 0
        return sum(1 for r in self.mapdb.stubs_in_area(self._explore_area_id)
                   if r["direction"] in self.CARDINAL_DIRS
                   and (r["from_vnum"], r["direction"]) not in self._explore_tried)

    def _explore_start(self):
        if self.map_backend != "sqlite" or self.mapdb is None:
            self.write_local("[explore] needs the sqlite map backend.",
                             "#cc6666")
            return
        v = self._sql_cur_vnum
        if v is None:
            self.write_local("[explore] location unknown - move a room, then "
                             "Explore.", "#cc6666")
            return
        area_id = self._bot_area_of(v)
        if area_id is None:
            self.write_local("[explore] this room has no charted area - can't "
                             "scope. Chart it first.", "#cc6666")
            return
        self._explore_on = True
        self._explore_area_id = area_id
        self._explore_area_name = self._area_name(area_id)
        self._explore_tried = set()
        self._explore_recharted = set()
        self._explore_flagged = set()
        self._explore_rated_tried = set()
        self._explore_rating = None
        self._explore_rate_back = None
        self._explore_plan = []
        self._explore_target = None
        self._explore_travel = False
        self._explore_travel_dest = None
        self._explore_travel_wait = 0
        self._explore_phase = "explore"     # plain Explore: no bootstrap
        self._explore_return_to = v          # walk back to the entrance when done
        self._explore_simple = False
        self._explore_start_n = self._explore_stub_count()
        # auto-enter charting (the only mode that writes the map)
        self._explore_entered_chart = not self._sql_charting
        if self._explore_entered_chart:
            self.sql_set_chart_mode(True)
        self.write_local(
            f"[explore] auto-charting '{self._explore_area_name}' - "
            f"{self._explore_start_n} open stub(s) (cardinal/ordinal only). "
            "Walks + probes each via the chart gate, then returns to this room. "
            "Explore off to stop. (Pauses in combat, stops on deadman; "
            "supervise it.)", "#66cc66")
        self._explore_audit_fix()       # correct any mischarts up front
        self._explore_flag_review()     # surface non-cardinal stubs up front
        self._explore_reschedule(0.6)

    def _explore_stop(self, why=""):
        left = self._explore_stub_count()
        done = max(0, self._explore_start_n - left)
        self._explore_on = False
        self._explore_plan = []
        self._explore_target = None
        self._explore_recharted = set()
        self._explore_flagged = set()
        self._explore_rated_tried = set()
        self._explore_rating = None
        self._explore_rate_back = None
        self._explore_travel = False
        if self._explore_job:
            self.root.after_cancel(self._explore_job)
            self._explore_job = None
        # Restore the charting state we started in. We turn charting OFF during
        # travel/return legs, so simply match the entry state: entered_chart
        # means we armed it (restore following); otherwise the user had it on
        # (re-arm it). Covers a mid-travel/return stop, not just a clean finish.
        if self._explore_entered_chart and self._sql_charting:
            self.sql_set_chart_mode(False)     # we armed it - restore following
        elif not self._explore_entered_chart and not self._sql_charting:
            self.sql_set_chart_mode(True)      # user had it on - re-arm it
        self._explore_entered_chart = False
        self.write_local(
            f"[explore] {why or 'off'} - charted {done} stub(s), "
            f"{left} left in '{self._explore_area_name}'.", "#aa88cc")

    def _explore_reschedule(self, secs):
        if self._explore_job:
            self.root.after_cancel(self._explore_job)
        self._explore_job = self.root.after(
            int(secs * 1000), self._explore_tick)

    def _explore_gate_idle(self):
        """True when the charting gate has drained the last move and is ready
        for the next - empty buffer, open gate, no pending no-BAD timer."""
        return (self._sql_gate_open and not self._sql_chart_buf
                and self._sql_gate_job is None)

    def _explore_next_stub(self):
        """Nearest reachable, unprobed stub in the area: (edges_to_it, row).
        Marks an unreachable stub tried so it isn't recomputed forever. Only
        cardinal/ordinal stubs are auto-probed; up/down/enter/out and special
        exits are left for manual review (see _explore_flag_review)."""
        cur = self._sql_cur_vnum
        # ONE shortest-path tree for the whole area, then pick the nearest
        # eligible stub by precomputed hop count - was a find_path (Dijkstra)
        # PER stub, which on a big 8-way grid ran tens of thousands of SQL
        # queries on the UI thread each pass and froze the client.
        dist, prev = self.mapdb.paths_from(cur)
        best = None
        best_d = None
        for row in self.mapdb.stubs_in_area(self._explore_area_id):
            if row["direction"] not in self.CARDINAL_DIRS:
                continue
            key = (row["from_vnum"], row["direction"])
            if key in self._explore_tried:
                continue
            fv = row["from_vnum"]
            d = 0 if fv == cur else dist.get(fv)
            if d is None:
                self._explore_tried.add(key)   # can't get there - skip it
                continue
            if best_d is None or d < best_d:
                best_d, best = d, row
        if best is None:
            return None
        fv = best["from_vnum"]
        edges = [] if fv == cur else self.mapdb.reconstruct_path(prev, cur, fv)
        return (edges, best)

    def _explore_restub(self, key, why):
        """Re-stub one mischarted edge so Explore re-walks it. Bounded by
        _explore_recharted (an edge that re-resolves the same way is corrected
        once, not forever). Returns 1 if it acted, else 0."""
        if key in self._explore_recharted:
            return 0
        self.mapdb.unlink_exit(*key)
        self._explore_recharted.add(key)
        self._explore_tried.discard(key)   # re-open for probing
        self.write_local(
            f"[explore] ERROR #{key[0]} {key[1]} - re-charting ({why})",
            "#ff6666")
        return 1

    def _explore_flag(self, keys, msg):
        """Report an unfixable inconsistency once (keyed on its exits)."""
        keys = list(keys)
        if all(k in self._explore_flagged for k in keys):
            return
        self._explore_flagged.update(keys)
        self.write_local(f"[explore] REVIEW {msg}", "#ffaa44")

    def _explore_audit_fix(self):
        """Detect & correct the maze mischarts Explore can adjudicate from
        room-ids + exit topology alone (short descs are never consulted):
          - two+ exits from ONE room to the same target (dupe_target_exits)
          - two+ rooms reaching one target via the SAME direction, where the
            target's reverse exit names the rightful owner (same_dir_collisions)
        The loser edge is re-stubbed for re-walking; ambiguous cases are
        flagged. Returns the number of edges re-stubbed (new stubs to walk)."""
        fixed = 0
        # (1) same room, two+ exits -> one target
        for from_v, to_v, rows in \
                self.mapdb.dupe_target_exits(self._explore_area_id):
            dirs = ",".join(r["direction"] for r in rows)
            cardinals = [r for r in rows
                         if r["direction"] in self.CARDINAL_DIRS]
            # only two+ plain compass exits is the unambiguous, re-walkable
            # mischart; a compass + special/enter to one room is ambiguous.
            if len(cardinals) >= 2:
                acted = sum(self._explore_restub(
                    (r["from_vnum"], r["direction"]),
                    f"also reaches #{to_v} via {dirs} from #{from_v}")
                    for r in cardinals)
                fixed += acted
                if acted:
                    continue
            self._explore_flag(
                [(r["from_vnum"], r["direction"]) for r in rows],
                f"#{from_v}: {dirs} all -> #{to_v} (can't auto-fix by hand)")
        # (2) two+ rooms reach one target via the same direction
        for d, to_v, froms in \
                self.mapdb.same_dir_collisions(self._explore_area_id):
            rd = REVERSE_DIR.get(d)
            rev = self.mapdb.get_exit(to_v, rd) if rd else None
            winner = rev["to_vnum"] if rev else None
            if d in self.CARDINAL_DIRS and winner in froms:
                # the target's reverse exit names the rightful owner; the
                # others charted the wrong room - re-stub them.
                acted = sum(self._explore_restub(
                    (f, d), f"#{to_v} {rd}->#{winner}, not #{f}")
                    for f in froms if f != winner)
                fixed += acted
                if acted:
                    continue
            self._explore_flag(
                [(f, d) for f in froms],
                f"{d}->#{to_v} claimed by {sorted(froms)} "
                "(reverse exit doesn't say which - resolve by hand)")
        return fixed

    def _explore_flag_review(self):
        """Report open stubs Explore won't auto-probe (up/down/enter/out and
        other non-cardinal exits), each one once, so they can be charted by
        hand. Returns the number newly flagged."""
        review = [r for r in self.mapdb.stubs_in_area(self._explore_area_id)
                  if r["direction"] not in self.CARDINAL_DIRS
                  and (r["from_vnum"], r["direction"])
                  not in self._explore_flagged]
        for r in review:
            self._explore_flagged.add((r["from_vnum"], r["direction"]))
        if review:
            dirs = ", ".join(f"#{r['from_vnum']} {r['direction']}"
                             for r in review)
            self.write_local(
                f"[explore] REVIEW: {len(review)} non-directional stub(s) "
                f"skipped - {dirs}", "#ffaa44")
        return len(review)

    def _explore_pick_frontier(self):
        """Nearest reachable unrated room (area_id NULL) hanging off an in-area
        room, that we haven't already walked to and rated. Returns
        (edges_to_it, vnum) or None. Unreachable ones are marked tried.

        Only rooms reached via a CARDINAL inbound edge are pursued: a one-way
        special exit (e.g. 'leave' into a foreign area) could strand us with no
        compass move back, so those are left for manual rating."""
        cur = self._sql_cur_vnum
        best = None
        seen = set()
        for r in self.mapdb.unrated_frontier(self._explore_area_id):
            t = r["to_vnum"]
            if r["direction"] not in self.CARDINAL_DIRS:
                continue
            if t in self._explore_rated_tried or t in seen:
                continue
            seen.add(t)
            edges = [] if t == cur else self.mapdb.find_path(cur, t)
            if edges is None:
                self._explore_rated_tried.add(t)    # can't reach it - skip
                continue
            if best is None or len(edges) < len(best[0]):
                best = (edges, t)
        return best

    def _explore_begin_rating(self, fr):
        """Walk to an unrated room (charting off, fast) so we can rate it; if
        we're already standing in it, rate now."""
        edges, t = fr
        self._explore_rating = t
        # remember how to back out if it rates foreign (reverse of the final,
        # cardinal entry edge - pick_frontier guarantees it's a compass move).
        self._explore_rate_back = (REVERSE_DIR.get(edges[-1]["direction"])
                                   if edges else None)
        if edges:
            self._explore_travel = True
            self._explore_travel_dest = t        # verified on arrival
            self._explore_travel_wait = 0
            if self._sql_charting:
                self.sql_set_chart_mode(False, announce=False)
            self._explore_plan = [c for e in edges for c in self._edge_cmds(e)]
            self.write_local(
                f"[explore] unrated #{t} - walking there to rate it "
                f"({len(self._explore_plan)} hop(s))", "#6699cc")
            self._explore_reschedule(0.2)
        else:
            self.write_local(f"[explore] rating unrated #{t} in place",
                             "#6699cc")
            self.sql_send_rating()
            self._explore_reschedule(0.6)

    def _explore_finish_rating(self):
        """A walked-to rating settled. If the room rated into THIS area it was a
        hole - it folds in and its stubs become reachable; otherwise it's a
        neighbour or an overland border (rated area-less, 'Caution is Advised')
        and we leave it. Either way we never re-rate it this run."""
        t = self._explore_rating
        self._explore_rating = None
        self._explore_rated_tried.add(t)
        room = self.mapdb.get_room(t)
        area = room["area_id"] if room else None
        if area == self._explore_area_id:
            self.write_local(
                f"[explore] rated #{t} into '{self._explore_area_name}' - "
                "folded in, its stubs are now in scope", "#66cc66")
            if not self._sql_charting:
                self.sql_set_chart_mode(True, announce=False)
        else:
            why = ("overland (Caution is Advised)" if self._sql_rate_overland
                   else f"in area #{area}" if area else "still unrated")
            # retreat the way we came (a travel leg, so charting stays off and
            # re-arms on arrival) - never strand in a foreign room.
            if self._explore_rate_back and self._sql_cur_vnum == t:
                self._explore_travel = True
                self._explore_plan = [self._explore_rate_back]
            self.write_local(
                f"[explore] #{t} is {why} - not part of "
                f"'{self._explore_area_name}', backing out", "#aa88cc")
        self._explore_rate_back = None

    def _explore_tick(self):
        self._explore_job = None
        if not self._explore_on:
            return
        if not (self.conn and self.conn.alive):
            self._explore_stop("disconnected"); return
        if self.deadman_tripped:
            self._explore_stop("deadman tripped"); return
        if not self._sql_charting and not self._explore_travel \
                and self._explore_rating is None \
                and self._explore_phase != "return":
            self._explore_stop("charting turned off"); return  # user toggled off
        if self.in_combat:
            self._explore_reschedule(1.0); return   # never walk off mid-fight
        if self._sql_cur_vnum is None or not self._explore_gate_idle():
            self._explore_reschedule(0.4); return   # let the gate settle
        # feed the current run one gated move at a time (combat-checked above)
        if self._explore_plan:
            self.send_line(self._explore_plan.pop(0))
            self._explore_reschedule(0.4); return
        # plan drained -> phase logic (chartwalk adds bootstrap + return)
        if self._explore_phase == "bootstrap":
            self._chartwalk_bootstrap_done(); return
        if self._explore_phase == "return":
            self._chartwalk_report("done")
            self._explore_stop("returned to start"); return
        # explore phase: the stub we were probing is now charted-or-failed
        if self._explore_target:
            self._explore_tried.add(self._explore_target)
            self._explore_target = None
        # A charting-off travel leg just finished. Before doing anything that
        # charts from this room (probe) or rates it, WAIT for the DDD to confirm
        # we actually reached the room we walked to - else a ~1s-late packet
        # leaves cur_vnum stale and we mischart/misrate from the wrong room.
        if self._explore_travel:
            dest = self._explore_travel_dest
            if dest is not None and self._sql_cur_vnum != dest:
                self._explore_travel_wait += 1
                if self._explore_travel_wait < 8:    # ~3s for the packet to land
                    self._explore_reschedule(0.4); return
                self.write_local(
                    f"[explore] travel expected #{dest} but at "
                    f"#{self._sql_cur_vnum} - recomputing from here", "#cc9933")
            self._explore_travel = False
            self._explore_travel_dest = None
            self._explore_travel_wait = 0
            if self._explore_rating is not None:
                if self._sql_cur_vnum == self._explore_rating:
                    self.sql_send_rating()           # rate the room we walked to
                    self._explore_reschedule(0.6); return
                # never reached it - abandon this rating, don't rate a wrong room
                self._explore_rated_tried.add(self._explore_rating)
                self._explore_rating = None
                self._explore_rate_back = None
                self._explore_reschedule(0.3); return
            if not self._sql_charting:
                self.sql_set_chart_mode(True, announce=False)
            self._explore_reschedule(0.4)           # next tick re-runs next_stub
            return
        # An on-demand rating is in flight: wait only for it to RESOLVE, then
        # classify (folded into this area vs foreign/overland) and move on. Gate
        # on _sql_rating_pending alone - that clears the instant sql_finish_room
        # sets the area. (_sql_rating_active lingers up to RATING_TIMEOUT=5s just
        # to swallow the trailing 'Monster class range' lines; waiting on it made
        # Explore sit idle for ~5s after the area was already known.)
        if self._explore_rating is not None:
            if self._sql_rating_pending:
                self._explore_reschedule(0.4); return
            self._explore_finish_rating()
            self._explore_reschedule(0.2); return
        nxt = self._explore_next_stub()
        if nxt is None:
            # Out of cardinal stubs - before declaring done: (1) sweep for
            # adjudicable mischarts, (2) walk to any unrated room and rate it
            # (folds in genuine holes; overland borders rate area-less and are
            # left). Either yields new work, so loop instead of finishing.
            if self._explore_audit_fix():
                self._explore_reschedule(0.2); return
            fr = self._explore_pick_frontier()
            if fr is not None:
                self._explore_begin_rating(fr); return
            self._explore_flag_review()             # surface non-cardinal stubs
            if self._explore_return_to is not None:
                self._chartwalk_begin_return()      # walk home + report
            else:
                self._explore_stop(f"'{self._explore_area_name}' complete")
            return
        edges, row = nxt
        if len(edges) > self._EXPLORE_FAR_HOPS:
            # Far jump across a mostly-mapped region: drop charting and fast-walk
            # there, then verify arrival before re-arming + probing (so we never
            # chart an edge from a stale room - the desync that mangled the map).
            self._explore_travel = True
            self._explore_travel_dest = row["from_vnum"]
            self._explore_travel_wait = 0
            if self._sql_charting:
                self.sql_set_chart_mode(False, announce=False)
            self._explore_plan = [c for e in edges for c in self._edge_cmds(e)]
            self.write_local(
                f"[explore] travel -> #{row['from_vnum']} (charting off, "
                f"{len(self._explore_plan)} hop(s))", "#6699cc")
            self._explore_reschedule(0.2)
            return
        # Near stub (<=2 hops, incl current room): keep charting ON and feed the
        # walk + probe as ONE gated plan. Each move waits for its room packet, so
        # the probe's from-room is always confirmed - no stale-position mischart.
        if not self._sql_charting:
            self.sql_set_chart_mode(True, announce=False)
        self._explore_target = (row["from_vnum"], row["direction"])
        plan = [c for e in edges for c in self._edge_cmds(e)]   # walk (gated)
        plan.extend(self._edge_cmds(row))                       # then probe
        self._explore_plan = plan
        where = (f"-> #{row['from_vnum']} {row['direction']}" if not edges
                 else f"~> #{row['from_vnum']} {row['direction']} "
                 f"({len(edges)} hop(s), gated)")
        self.write_local(
            f"[explore] {where} ({self._explore_stub_count()} left)", "#6699cc")
        self._explore_reschedule(0.2)

    # ------------------------------------------------ queue pump
    def poll_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind in ("line", "prompt"):
                    _, spans, clean = item
                    swallowed = (kind == "line") and (
                        self.sql_rating_capture(clean)
                        if self.map_backend == "sqlite"
                        else self.rating_capture(clean))
                    gagged = swallowed or (
                        (kind == "line") and self.is_gagged(clean))
                    if not gagged:
                        self.write_spans(spans,
                                         newline=(kind == "line"))
                    if self.log_file:
                        if not swallowed:
                            self.log_file.write(
                                clean + ("\n" if kind == "line" else ""))
                        else:
                            # rating burst is swallowed from the display; tee it
                            # (AREA NAME / 'Caution is Advised [Mud]' / class
                            # range) so the log shows the ground-truth that
                            # drives area_id + rate-on-demand. 'RATE ' prefix
                            # keeps it filterable.
                            self.log_file.write("RATE " + clean + "\n")
                    if kind == "line" and not swallowed:
                        self.capture_statline(clean)
                        if self.guild.lower() == "necromancers":
                            self.necro_scan_line(clean)
                            if self._reagents_target is not None:
                                self._reagents_have.update(
                                    necro.parse_reagents_line(clean))
                        elif self.guild.lower() == "bladesingers":
                            self.blade_scan_line(clean)
                        elif self.guild.lower() == "changelings":
                            self.changeling_scan_line(clean)
                        elif self.guild.lower() == "vikings":
                            self.viking_scan_line(clean)
                            self._missionlist_scan_line(clean)
                            self._vnlist_scan_line(clean)
                        self.track_combat_line(clean)
                        if self.map_backend == "sqlite":
                            self.vscan_scan_line(clean)
                        if self._bot_on or self._run_on or self._chaossea_on:
                            self._scan_pwho(clean)       # seed party whitelist
                            self._resume_scan_line(clean)  # post-kill hold
                        if self._bot_on and self._bot_waiting_room \
                                and not self.in_combat:
                            self._bot_scan_markers(spans, clean)
                        if self._chaossea_on and self._chaossea_waiting_room \
                                and not self.in_combat:
                            self._chaossea_scan_markers(spans, clean)
                        if self._chaossea_on and self._chaossea_examine_pending:
                            self._chaossea_examine_scan_line(clean)
                        if self._run_precheck:
                            self._run_precheck_scan_line(clean)
                        elif self._run_on:
                            # Unlike Bot/Chaossea, Run must keep buffering
                            # room text even while self.in_combat reads
                            # True - that flag can be stale (left set from
                            # a fight abandoned in a previous room), which
                            # would otherwise silently drop the very
                            # 'attacking you!' line _run_room_attackers
                            # needs to catch on room entry.
                            self._run_scan_markers(spans, clean)
                        if self._macro_on:
                            self._macro_scan_line(clean)
                        if self.markers_debug:
                            self._report_line_markers(spans, clean)
                        self.maybe_handshake(clean)
                        self.check_auth_failure(clean)
                        self.check_triggers(clean)
                        self.call_hook("on_line", clean)
                    if kind == "prompt":
                        if self.guild.lower() == "changelings":
                            self.changeling_scan_line(clean)
                        if self._bot_on:
                            self._bot_on_prompt()
                        if self._chaossea_on:
                            if self._chaossea_examine_pending:
                                self._chaossea_finish_examine()
                            else:
                                self._chaossea_on_prompt()
                elif kind == "mip":
                    self.handle_mip(item[1], item[2])
                elif kind == "status":
                    self.set_status(item[1])
                elif kind == "event":
                    self.handle_event(item[1])
                elif kind == "maploaded":
                    self.on_maploaded(item[1], item[2])
        except queue.Empty:
            pass
        self.root.after(30, self.poll_queue)

    def on_maploaded(self, tmap, extra):
        if tmap is None:
            self.write_local(f"[map] {extra}", "#cc6666")
            return
        self.tmap = tmap
        self.speedruns = extra
        self.locator = mapdata.Locator(self.tmap)
        self.load_landmarks()
        patched = sum(1 for r in tmap.rooms.values() if r.patched)
        self.write_local(
            f"[map] {len(self.tmap.rooms)} rooms "
            f"({patched} from patches), "
            f"{len(self.speedruns)} speedruns, "
            f"{len(self.landmarks)} landmarks loaded.", "#66cc66")
        for e in tmap.patch_errors:
            self.write_local(f"[map] patch: {e}", "#cc9933")
        self.map_locate()

    def maybe_handshake(self, clean):
        if self.mip_sent or not self.mip_enabled:
            return
        if "elcome" in clean:
            self.conn.send(f"3klient {self.mip_pin:05d}~Po1.01")
            self.mip_sent = True
            self.write_local(f"[MIP handshake sent, pin "
                             f"{self.mip_pin}]", "#557755")

    def handle_event(self, ev):
        if ev == "CONNECTED":
            self.connected_at = time.time()
            self.set_status(f"connected to {self.host}:{self.port} "
                            f"as {self.display_name}")
            self.write_local(f"*** Connected to {self.host}:"
                             f"{self.port} ***", "#66cc66")
            if self.character:
                self.send_line(self.character, manual=True)
            self.call_hook("on_connect")
        elif ev == "DISCONNECTED":
            self.stat1 = self.stat2 = ""
            self.viking_spells = ""
            self.set_status("disconnected")
            self.write_local("*** Disconnected ***", "#cc6666")
        elif ev == "ECHO_OFF":
            self.echo_on = False
            self.entry.configure(show="*")
            if not self.password_sent:
                pw, prompted = self.get_login_password()
                if pw:
                    self.password_sent = True
                    self._pending_store_pw = pw if prompted else None
                    self.conn.send(pw)
                    self.write_local("[password sent]", "#557755")
        elif ev == "ECHO_ON":
            self.echo_on = True
            self.entry.configure(show="")

    # ------------------------------------------------ MIP dispatch
    def handle_mip(self, tag, data):
        if self.mipraw:
            entry = f"{tag} {data}"
            self.mipraw_buf.append(entry)
            self.write_local("MIP> " + entry, "#55aacc")
        # When a full session log is open, tee MIP packets into it inline
        # with the text (they're stripped from the display stream, so they
        # would otherwise never reach the log). The "MIP " prefix keeps them
        # distinguishable - filter back out with a grep on '^MIP '.
        if self.log_file:
            try:
                self.log_file.write(f"MIP {tag} {data}\n")
            except (OSError, ValueError):
                pass
        decl = self.mip_registry.get(tag)
        if decl:
            handler = getattr(self, "mip_" +
                              decl.get("handler", ""), None)
            if handler:
                handler(data, decl)
            else:
                self.last_unknown_mip = (f"{tag} -> unknown handler "
                                         f"{decl.get('handler')!r}")
                self.update_info()
        elif tag not in ("AAA", "AAB", "AAD"):
            self.last_unknown_mip = f"{tag} {data[:48]}"
            self.update_info()
        self.call_hook("on_mip", tag, data)

    # --- handler implementations (names referenced by mud.json) ---
    def mip_vitals_composite(self, data, _decl):
        if not self._logged_in:
            self.confirm_login()
        if not self.guild_login_sent:
            self.guild_login_sent = True
            # Mud-wide setup first (asets, DISPLAY_ROOMID) so the room
            # markers + mapping work before anything guild-specific runs.
            if self.setup_commands:
                self.write_local(f"[{self.mud} setup: "
                                 f"{len(self.setup_commands)} "
                                 "command(s)]", "#557755")
                for c in self.setup_commands:
                    self.send_line(c, manual=True)
            if self.login_commands:
                self.write_local(f"[guild init: "
                                 f"{len(self.login_commands)} "
                                 "command(s)]", "#557755")
                for c in self.login_commands:
                    self.send_line(c, manual=True)
            self._send_on_activate()
        upd = parse_composite(data)
        if self.guild.lower() == "changelings":
            # The FFF E field (parsed as gp1 by the global map) is really LIVE
            # Stamina - route it to gp2 so the Stamina bar tracks every round
            # AND its regen between fights. Protoplasm (gp1) has no FFF field;
            # it comes from the prompt (changeling.parse_prompt), so drop any
            # stray gp1 here. E is a 0-100% value, so its max is 100.
            if "gp1" in upd:
                upd["gp2"] = upd.pop("gp1")
                upd["gp2max"] = 100.0
            upd.pop("gp1max", None)
        self.vitals.update(upd)
        for f in ("hp", "sp", "gp1", "gp2"):
            if f in upd and upd[f] > self.vitals_max.get(f, 0):
                self.vitals_max[f] = upd[f]
                self._maxes_dirty = True
        if self.guild.lower() == "necromancers" and "gp2" in upd:
            # 3k necros have no 2nd guild pool; the FFF G field (parsed as gp2)
            # carries the guild RESET % instead. 3k sends no hpbar1 I-field
            # (unlike 3s), so this is the always-on source for reset%.
            self.vars["reset"] = upd["gp2"]
            self.update_info()
        if "round" in upd:
            self._fff_combat = True
            # Only let a round tick touch rounds/in_combat if we actually
            # have a tracked enemy - some classes emit a bare round/N update
            # outside of any fight (bladesinger post-kill revalrie, viking
            # vrest), which would otherwise (a) re-arm in_combat with no
            # "enemy" packet ever following to clear it again (the K field
            # is the sole authority for ending a fight - see _run_tick), and
            # (b) bleed that mechanic's own round count into self.rounds,
            # inflating the NEXT fight's displayed/recorded round count
            # since it's never reset to 0 except by a kill-text match.
            if len(self.vitals.get("enemy", "") or "") > 1:
                self.rounds = upd["round"]
                self.in_combat = True
        if "enemy" in upd:
            self._fff_combat = True
            if len(upd["enemy"]) > 1:
                self.in_combat = True
                self.check_required_fields()
            else:
                self.in_combat = False
                self.vitals["enemy"] = ""
                self.vitals.pop("enemycond", None)
        if "hpbar1" in upd and upd["hpbar1"].strip():
            stripped = strip_mip_colors(upd["hpbar1"]).strip()
            guild = self.guild.lower()
            if guild == "necromancers":
                # The necro hpbar1 is the guild Status text, not a delta -
                # route it to the Status parser, not parse_deltas (which
                # would dump the whole string into last_deltas).
                self._necro_hpbar_mip(stripped)
            elif guild == "gentech":
                # Gentech's hpbar1 (I) and hpbar2 (J) are guild status text;
                # both stay in self.vitals and are rendered directly in the
                # info pane (_render_gentech_hpbar). Not deltas.
                pass
            elif guild == "changelings":
                # Changeling I-field (Flux/Density/FF/form) -> the status bar
                # under the hpbars; the info pane still gets the bioplast count.
                self.changeling_status = stripped
                self.show_status()
            else:
                self.last_deltas = self.parse_deltas(stripped)
                if guild == "vikings":
                    self.vitals.update(viking.parse_hpbar(upd["hpbar1"]))
            self.update_info()
        elif self.guild.lower() == "changelings" and "hpbar2" in upd:
            # J-only packet (bioplast count changed, no I field) - refresh the
            # info pane so the bioplast line tracks gains/uses immediately.
            self.update_info()
        self.update_vitals()
        self.call_hook("on_vitals", dict(self.vitals))

    def mip_room(self, data, _decl):
        if self.map_backend == "sqlite":
            self.sql_on_bad(data)
            return
        raw_room = data.strip()
        m = re.match(r"^(.*?)\s*\(([^)]*)\)~(\d+)\s*$", raw_room)
        if m:
            self.room = m.group(1)
            self.room_vnum = int(m.group(3))
            self.exits = [e.strip() for e in m.group(2).split(",")
                          if e.strip()]
            self.map.update_room(room=self.room, exits=self.exits)
        else:
            self.room = raw_room
            self.room_vnum = None
            self.map.update_room(room=self.room)
        self.map_locate(vnum=self.room_vnum)
        self._last_move = None      # consumed
        self.update_info()

    def mip_exits(self, data, _decl):
        if self.map_backend == "sqlite":
            self.sql_on_ddd(data)
            return
        parts = [p for p in re.split(r"[~ ]+", data.strip()) if p]
        if parts and parts[-1].isdigit():
            self.room_vnum = int(parts[-1])
            parts = parts[:-1]
        if parts:
            self.exits = parts
            self.map.update_room(exits=parts)
        self.update_info()

    def mip_uptime(self, data, _decl):
        self.uptime = data.strip()

    def mip_reboot_eta(self, data, _decl):
        self.reboot_eta = data.strip()

    def mip_mudlag(self, data, _decl):
        self.mudlag = data.strip()

    def _chat_ts(self):
        """[HH:MM:SS] prefix for the chat/tell pane only."""
        return time.strftime("[%H:%M:%S] ")

    def mip_chat(self, data, _decl):
        parts = data.split("~")
        chan = parts[0] if parts else "?"
        text = parts[-1] if parts else data
        line = f"[{chan}] {text}"
        if self.chat_is_gagged(line):
            return
        self._web_record(self._web_chats, line)
        self.write_local(self._chat_ts() + line, "#cc99ee", widget=self.chat)

    def mip_tell(self, data, _decl):
        """BAB payload = FLAG~NAME~TEXT. Confirmed via two real two-party
        captures (logs/mip_20260620_171046.log) against the plain-text lines
        bracketing each: empty FLAG is an INCOMING tell (NAME = who told
        you, e.g. "~Horyd~I wish..." alongside plain "Horyd tells you: I
        wish..."); a non-empty FLAG (e.g. "x") is the echo of your own
        OUTGOING tell (NAME = who you told, e.g. "x~Horyd~me too..."
        alongside plain "You tell Horyd: me too..."). A same-name self-tell
        capture earlier looked consistent with the OPPOSITE mapping too
        (its two packets had no name-based way to confirm which was which) -
        trust this asymmetric data over that one."""
        parts = data.split("~")
        flag = parts[0] if parts else ""
        name = parts[1] if len(parts) > 2 else "?"
        text = parts[-1] if parts else data
        if self.chat_is_gagged(text):
            return
        if flag:
            line = f"You tell {name}: {text}"
        else:
            line = f"{name} tells you: {text}"
            play_sound(self.setting("tell_sound", "beep"))
            self._push_ntfy_tell(line)
        self._web_tells.append({"t": time.time(), "line": line,
                                "incoming": not flag})
        self.write_local(self._chat_ts() + line, "#ffdd88", widget=self.chat)

    def _push_ntfy_tell(self, line):
        """Fire-and-forget push for an incoming tell only (spec:
        docs/superpowers/specs/2026-06-21-web-dashboard-design.md) - the
        phone-dashboard scenario this exists for is a wiz "bot check"
        landing while the user is away from the PC. Must never block the
        Tk main thread: urlopen is synchronous, so the actual request runs
        on a daemon thread, not here."""
        topic = self.setting("ntfy_topic", None)
        if not topic:
            return
        threading.Thread(target=self._post_ntfy, args=(topic, line),
                         daemon=True).start()

    @staticmethod
    def _post_ntfy(topic, text):
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=text.encode("utf-8"), method="POST")
            urllib.request.urlopen(req, timeout=3).close()
        except Exception:
            pass    # a network blip must never matter to the caller

    def mip_social(self, data, _decl):
        """BAG payload = FLAG~NAME~TEXT (social emotes, vendor/NPC
        transaction text, and unnamed narrated events all ride this tag).
        Unlike BAB's tell, FLAG does not track direction consistently here
        (confirmed against 5 real examples, logs/mip_20260620_202329.log +
        historical captures) - but a text-shape rule fits all of them: if
        TEXT already starts with "you " it's a self-contained sentence from
        your own perspective (just capitalize it), otherwise NAME is the
        missing subject and needs prepending:
          "~Normal~nods to you."        -> "Normal nods to you."
          "x~Orain~shoos you out..."    -> "Orain shoos you out..."
          "x~Orain~you greet Orain..."  -> "You greet Orain..."
          "~~Everything slowly fades.." -> "Everything slowly fades.."
        (last one has no NAME at all - shown as-is)."""
        parts = data.split("~")
        name = parts[1] if len(parts) > 2 else (parts[0] if parts else "")
        text = parts[-1] if parts else data
        if self.chat_is_gagged(text):
            return
        if text[:4].lower() == "you ":
            line = "You" + text[3:]
        elif name:
            line = f"{name} {text}"
        else:
            line = text
        self._web_record(self._web_chats, line)
        self.write_local(self._chat_ts() + line, "#aabb88", widget=self.chat)

    def mip_caption(self, data, _decl):
        self.root.title(f"KatMUD \u2014 {data.strip()}")

    def mip_reboot_notice(self, _data, _decl):
        self.write_local("*** MUD reports REBOOT ***", "#ff6666")
        self.write_local("*** MUD reports REBOOT ***", "#ff6666",
                         widget=self.chat)

    MERC_INT_FIELDS = (
        "hp", "hpmax", "stamina", "staminamax", "ap", "apmax",
        "target_hp_pct", "stamina_regen", "ap_regen", "perm_level",
        "perm_xp", "perm_xp_next", "inst_level", "inst_xp", "inst_xp_next",
        "cost_per_round", "funds", "spent")
    MERC_FIELDS = (
        "hp", "hpmax", "stamina", "staminamax", "ap", "apmax", "name",
        "target_hp_pct", "stamina_regen", "ap_regen", "perm_level",
        "perm_xp", "perm_xp_next", "inst_level", "inst_xp", "inst_xp_next",
        "cost_per_round", "damage_type", "following", "funds", "spent",
        "target_name", "abilities")

    def mip_mercenary(self, data, _decl):
        """Mercenary system feed (mud-wide BBC tag, ~/supporting docs/
        mercenaries.txt): 24 tilde-delimited positional fields, resent every
        combat round. Not guild-specific - any character can hire a merc."""
        parts = data.split("~")
        raw = dict(zip(self.MERC_FIELDS, parts))
        merc = {}
        for key in self.MERC_INT_FIELDS:
            try:
                merc[key] = int(raw.get(key, ""))
            except ValueError:
                pass
        merc["name"] = raw.get("name", "").strip()
        merc["target_name"] = raw.get("target_name", "").strip()
        merc["damage_type"] = strip_mip_colors(
            raw.get("damage_type", "")).strip()
        merc["abilities"] = strip_mip_colors(
            raw.get("abilities", "")).strip()
        merc["following"] = raw.get("following") == "1"
        if not merc["name"]:
            return
        self.merc_state = merc
        self.update_info()

    def mip_viking(self, data, _decl):
        """All Viking guild feeds arrive under the BBE tag as KEY^^VALUE
        pairs, chunked across packets - so merge by key into one running
        state dict and refresh the status window if it's open."""
        upd = viking.parse_bbe(data)
        if not upd:
            return
        self.viking_state.update(upd)
        if "VMAPH" in upd:
            self._viking_pos_fire()
        vit = viking.vitals_from_state(upd)
        if vit:
            self.vitals.update(vit)
            self.update_vitals()
        if "STFX" in upd or "DALER" in upd:
            self.viking_spells = viking.active_spells(self.viking_state)
            self.show_status()
        if self.viking_win is not None and \
                self.viking_win.winfo_exists():
            self.viking_win.update_state(self.viking_state)

    def _viking_raise_with_main(self, event=None):
        """Anchor the Viking status window to the main window: when the main
        window gains focus (the user clicked it), lift the status window too
        so both surface together. lift() doesn't move keyboard focus, so the
        main window stays active."""
        if event is not None and event.widget is not self.root:
            return                      # ignore child-widget focus churn
        if self.viking_win is not None and self.viking_win.winfo_exists():
            try:
                self.viking_win.lift()
            except tk.TclError:
                pass

    def open_viking_status(self):
        if self.viking_win is not None and \
                self.viking_win.winfo_exists():
            self.viking_win.deiconify()
            self.viking_win.lift()
            self.viking_win.update_state(self.viking_state)
            return
        self.viking_win = viking.VikingStatus(
            self.root,
            fonts={"mono": self.small_font, "mono_bold": self.small_bold},
            on_close=self._viking_closed,
            walk_cb=self.viking_walk_to,
            geometry=self.profiles_data.get("settings", {}).get(
                "viking_geometry"))
        self.viking_win.update_state(self.viking_state)
        if self.viking_skills:          # restore the last-read skill costs
            self.viking_win.set_vskills(self.viking_skills)
        if self._viking_focus_bind is None:
            self._viking_focus_bind = self.root.bind(
                "<FocusIn>", self._viking_raise_with_main, add="+")

    def _viking_closed(self):
        # _closed() calls this before destroy(), so geometry is still valid.
        try:
            if self.viking_win is not None and \
                    self.viking_win.winfo_exists():
                s = self.profiles_data.setdefault("settings", {})
                s["viking_geometry"] = self.viking_win.geometry()
                profiles.save(self.profiles_data)
        except tk.TclError:
            pass
        if self._viking_focus_bind is not None:
            try:
                self.root.unbind("<FocusIn>", self._viking_focus_bind)
            except tk.TclError:
                pass
            self._viking_focus_bind = None
        self.viking_win = None

    # ------------------------------------------- viking map navigation
    def _viking_after_position(self, cb, settle=0.2, timeout_ms=3000):
        """Run cb once a FRESH guild-map position has arrived: the next
        VMAPH packet at least `settle` seconds after arming (so a packet
        already in flight from before the last move/leave doesn't fire it on
        a stale cell). A fallback timer runs cb regardless after timeout_ms,
        so a VN leg can't hang if no VMAPH comes. Inside a lineage city the
        guild map sends no VMAPH, so after a `leave` the next one is reliably
        the post-leave cell - the position VN must path the next leg from."""
        if self._viking_pos_job:
            self.root.after_cancel(self._viking_pos_job)
        self._viking_pos_cb = cb
        self._viking_pos_armed = time.time()
        self._viking_pos_job = self.root.after(
            timeout_ms, self._viking_pos_timeout)
        self._viking_pos_settle = settle

    def _viking_pos_fire(self):
        """A VMAPH arrived: run the pending position callback if one is armed
        and the settle window has passed."""
        if not self._viking_pos_cb:
            return
        if time.time() - self._viking_pos_armed < \
                getattr(self, "_viking_pos_settle", 0.6):
            return                       # too soon - likely an in-flight pkt
        cb = self._viking_pos_cb
        self._viking_pos_cb = None
        if self._viking_pos_job:
            self.root.after_cancel(self._viking_pos_job)
            self._viking_pos_job = None
        cb()

    def _viking_pos_timeout(self):
        self._viking_pos_job = None
        cb = self._viking_pos_cb
        self._viking_pos_cb = None
        if cb:
            cb()

    def viking_marks(self):
        """User's custom coordinate landmarks: {name: [x, y]}."""
        return dict(self.setting("viking_landmarks", {}) or {})

    def viking_walk_to(self, gx, gy, on_done=None):
        """Pathfind from the current map position to cell (gx, gy) over
        the MEE/MES graph and send the n/s/e/w moves. Used by both
        click-to-move and #go on the guild map. on_done (if given) fires
        once the walk reaches the cell - the hook VN uses to chain enter /
        fetch after arrival. It does NOT fire if the walk can't start
        (no path / position unknown), so a botched leg halts the errand
        rather than blundering on."""
        graph = viking.build_graph(self.viking_state)
        if not graph:
            self.write_local("[viking nav] no map loaded - enable "
                             "'vtoggle mip_map'.", "#cc6666")
            return
        cols, rows, mee, mes, player = graph
        if player[0] is None:
            self.write_local("[viking nav] position unknown.", "#cc6666")
            return
        if not (0 <= gx < cols and 0 <= gy < rows):
            self.write_local("[viking nav] target off the map.",
                             "#cc6666")
            return
        moves = viking.pathfind(cols, rows, mee, mes, player, (gx, gy))
        if moves is None:
            self.write_local(f"[viking nav] no path to ({gx},{gy}).",
                             "#cc6666")
            return
        self.cancel_walk(quiet=True)        # clears any old _walk_done_cb
        if not moves:
            self.write_local("[viking nav] already there.", "#aa88cc")
            if on_done:
                on_done()
            return
        self.walk = moves
        self._walk_done_cb = on_done        # set AFTER cancel_walk wipes it
        self.write_local(f"[viking map: {len(moves)} steps to "
                         f"({gx},{gy})]", "#aa88cc")
        self._walk_step()

    def viking_go(self, name, on_done=None):
        """Resolve a name against VMAPL POIs + custom marks and walk
        there. Returns True if it handled the request (a matching landmark
        existed); on_done fires on arrival."""
        if self.guild.lower() != "vikings":
            return False
        marks = viking.map_landmarks(self.viking_state, self.viking_marks())
        if not marks:
            return False
        key = name.lower()
        hit = marks.get(key)
        if not hit:
            cands = [k for k in marks if k.startswith(key)]
            if len(cands) == 1:
                hit = marks[cands[0]]
        if not hit:
            return False
        self.viking_walk_to(hit[0], hit[1], on_done=on_done)
        return True

    def add_viking_mark(self, name):
        graph = viking.build_graph(self.viking_state)
        if not graph or graph[4][0] is None:
            self.write_local("[viking nav] position unknown - can't "
                             "mark here.", "#cc6666")
            return
        x, y = graph[4]
        marks = self.viking_marks()
        marks[name.lower()] = [x, y]
        self.set_setting("viking_landmarks", marks)
        self.write_local(f"[viking mark '{name}' = ({x},{y})]", "#66cc66")

    def cmd_vn(self, arg):
        """`VN <id> <start> <destination>` - run one Viking newbie fetch
        errand end to end. <start>/<destination> are guild-map landmarks
        (the lineage cities saved via vmark / viking_landmarks). Sequence:

            leave; vmission newbie accept <id>
            Go <start>; enter; vmission newbie fetch; leave
            Go <destination>; enter; vmission newbie submit; leave
            vmission newbie

        The two `Go` legs walk the guild map asynchronously, so the enter/
        fetch and enter/submit steps are chained on the walk's arrival
        callback rather than sent up front (which would race ahead of the
        paced moves). Each `Go` is also gated on a fresh guild-map position
        after its preceding `leave` - leaving a lineage city repositions you
        on the map, and pathing before that arrives walks from a stale cell
        (the bug where leg two wandered to midgard). A leg that can't be
        walked aborts the errand."""
        if self.guild.lower() != "vikings":
            self.write_local("VN is a Vikings guild command.", "#cc9933")
            return
        bits = arg.split()
        if len(bits) != 3:
            self.write_local("Usage: VN <id> <start> <destination>",
                             "#cc6666")
            return
        mid, start, dest = bits

        def at_dest():
            for c in ("enter", "vmission newbie submit", "leave",
                      "vmission newbie"):
                self.send_line(c)
            self.write_local(f"[VN {mid}: submitted at {dest}]", "#66cc66")

        def go_dest():
            if not self.viking_go(dest, on_done=at_dest):
                self.write_local(f"[VN aborted: no guild-map landmark "
                                 f"'{dest}']", "#cc6666")

        def at_start():
            for c in ("enter", "vmission newbie fetch", "leave"):
                self.send_line(c)
            self.write_local(f"[VN {mid}: fetched at {start}, awaiting map "
                             f"to head to {dest}]", "#aa88cc")
            self._viking_after_position(go_dest)

        self.send_line("leave")
        self.send_line(f"vmission newbie accept {mid}")
        self.write_local(f"[VN {mid}: accepted, heading to {start}]",
                         "#aa88cc")
        # First leg: you're already on the guild map (the leg-two midgard bug
        # was specifically pathing before the post-city-leave position
        # arrived), so path it straight away.
        if not self.viking_go(start, on_done=at_start):
            self.write_local(f"[VN aborted: no guild-map landmark "
                             f"'{start}']", "#cc6666")

    # ---- Necromancer Track tracker (see necro.py) ----------------------
    def _tracked_key(self):
        g = self.guild.lower() or "none"
        return f"tracked_{self.mud.lower()}_{g}"

    def _save_tracked(self):
        self.set_setting(self._tracked_key(), self.tracked)

    def cmd_track(self, arg):
        """`Track` lists tracked items; `Track <name> [low]` starts/updates
        one (low = highlight dim-red when the count drops below it). <name>
        may be multi-word (a reagent like 'black pearls'); a trailing
        integer is the threshold. Counts come from `powers`/`gs` readouts."""
        blade_guild = self.guild.lower() == "bladesingers"
        bits = arg.split()
        if not bits:
            if not self.tracked:
                eg = ("Track soul aegis" if blade_guild
                      else "Track drain 30")
                self.write_local(f"Tracking nothing. Track <name> [low] "
                                 f"- e.g. {eg}", "#cc9933")
                return
            self.write_local("Tracking:")
            if blade_guild:
                self.write_local(f"  (spendable GXP: "
                                 f"{blade.fmt_gxp(self.blade_available)})")
            for name in sorted(self.tracked):
                if blade_guild:
                    cost = self.blade_skill_costs.get(name)
                    if cost is None and name not in self.blade_skill_costs:
                        self.write_local(f"  {name}: ? (read `skills`)")
                    elif cost is None:
                        self.write_local(f"  {name}: maxed")
                    elif self.blade_available is None:
                        self.write_local(
                            f"  {name}: cost {blade.fmt_gxp(cost)}")
                    else:
                        need = cost - self.blade_available
                        self.write_local(
                            f"  {name}: " + ("READY" if need <= 0
                            else f"need {blade.fmt_gxp(need)}")
                            + f" ({blade.fmt_gxp(cost)})")
                    continue
                low = self.tracked[name]
                val = self.necro_counts.get(name)
                self.write_local(
                    f"  {name}: {'?' if val is None else val}"
                    + (f"  (low {low})" if low is not None else ""))
            return
        low = None
        if bits[-1].lstrip("-").isdigit():
            low = int(bits[-1])
            bits = bits[:-1]
        name = " ".join(bits).lower()
        if not name:
            self.write_local("Usage: Track <name> [low]", "#cc6666")
            return
        self.tracked[name] = low
        self._save_tracked()
        self.write_local(
            f"[tracking {name}" + (f", low {low}" if low is not None
                                   else "") + "]", "#66cc66")
        self.update_info()

    def cmd_untrack(self, arg):
        name = arg.strip().lower()
        if name in self.tracked:
            del self.tracked[name]
            self._save_tracked()
            self.write_local(f"[untracked {name}]", "#aa88cc")
            self.update_info()
        else:
            self.write_local(f"Not tracking '{name}'.", "#cc9933")

    # ---- Reagents restock (Necromancer) --------------------------------
    _REAGENTS_TARGET = 999          # buy each reagent up to this
    _REAGENTS_SKIP = {"bloodmoss"}  # not bought via the shop

    def cmd_reagents(self, arg):
        """Necromancer: `gs`, then buy each reagent up to a target (default
        999, override `Reagents <n>`), skipping bloodmoss. Buy amounts come
        from the FRESH gs counts captured into self._reagents_have."""
        if self.guild.lower() != "necromancers":
            self.write_local("Reagents is a Necromancer command.", "#cc9933")
            return
        a = arg.strip()
        target = self._REAGENTS_TARGET
        if a:
            if not a.isdigit():
                self.write_local("Usage: Reagents [target]  (e.g. Reagents "
                                 "999)", "#cc6666")
                return
            target = int(a)
        self._reagents_have = {}
        self._reagents_target = target
        self.send_line("gs")
        self.write_local(f"[reagents] reading gs, restocking to {target} "
                         "(skipping bloodmoss)...", "#88cc88")
        delay = self.setting("reagents_delay_ms", 1200)
        self.root.after(int(delay), self._reagents_finish)

    def _reagents_finish(self):
        target = self._reagents_target
        self._reagents_target = None        # stop capturing
        if target is None:
            return
        have = self._reagents_have
        if not have:
            self.write_local("[reagents] no gs reagent lines seen - try a "
                             "longer reagents_delay_ms.", "#cc9933")
            return
        bought = 0
        for name in sorted(have):
            if name in self._REAGENTS_SKIP:
                continue
            need = target - have[name]
            if need > 0:
                self.send_line(f"buy {need} {name}")
                bought += 1
        if bought:
            self.write_local(f"[reagents] bought {bought} reagent type(s) "
                             f"toward {target}; gs to verify.", "#88cc88")
        else:
            self.write_local(f"[reagents] all reagents already at {target}+.",
                             "#88cc88")

    _NECRO_AUTO_COOLDOWN = 20      # seconds between repeats of an auto-command

    def _necro_prompt(self, p):
        """Apply a parsed status prompt: feed the vitals bars (NP especially -
        FFF rarely sends gp1), stash the Status fields in self.vars, expose
        the corpse count to the tracker, then run the auto-commands."""
        if not p:
            return
        for src, dst in (("np", "gp1"), ("npmax", "gp1max"),
                         ("hp", "hp"), ("hpmax", "hpmax"),
                         ("sp", "sp"), ("spmax", "spmax")):
            if src in p:
                self.vitals[dst] = p[src]
        if self.vitals.get("gp1", 0) > self.vitals_max.get("gp1", 0):
            self.vitals_max["gp1"] = self.vitals["gp1"]
            self._maxes_dirty = True
        self.update_vitals()
        for k in ("worth", "protection", "veil", "reset", "circle",
                  "corpses"):
            if k in p:
                self.vars[k] = p[k]
        if "corpses" in p:
            self.necro_counts["corpses"] = p["corpses"]
        if "circle" in p:
            tp = necro.tier_progress(p["circle"])
            if tp:
                self.vars["tier"] = tp
            else:
                self.vars.pop("tier", None)
        self._necro_autostatus()
        self.necro_status = necro.format_status_bar(self.vars)
        self.show_status()
        self.update_info()

    def _necro_hpbar_mip(self, stripped):
        """The FFF hpbar I-field (always-on MIP): worth / protection /
        circle(+tier) / glamor / tport. This is what lets the Status block
        work WITHOUT piping the text hpbar to the main window. reset%,
        corpses and veil are NOT in this feed (text prompt only). Caller
        runs update_info."""
        p = necro.parse_hpbar(stripped)
        for k in ("worth", "protection", "circle", "glamor", "tport"):
            if k in p:
                self.vars[k] = p[k]
        if "circle" in p:
            tp = necro.tier_progress(p["circle"])
            if tp:
                self.vars["tier"] = tp
            else:
                self.vars.pop("tier", None)
        self._necro_autostatus()
        self.necro_status = necro.format_status_bar(self.vars)
        self.show_status()

    def _necro_autostatus(self):
        """Edge-triggered guild upkeep: re-send the fix command when a Status
        field is bad, no more than once per cooldown (so a command that
        didn't take - no reagents, etc. - retries but never spams). Gated by
        the `necroguard` Toggle; respects deadman (manual=False) so it stays
        quiet while the player is idle/away."""
        if not self.necro_guard or self.guild.lower() != "necromancers":
            return
        v = self.vars
        self._necro_auto("worth", v.get("worth", 125) < 125, "con 100")
        self._necro_auto("prot", v.get("protection", True) is False,
                         "protection")
        self._necro_auto("veil", v.get("veil", True) is False, "veil")

    def _necro_auto(self, key, bad, cmd):
        if not bad:
            self._necro_auto_last.pop(key, None)   # cleared: re-fire on relapse
            return
        now = time.time()
        if now - self._necro_auto_last.get(key, 0) >= self._NECRO_AUTO_COOLDOWN:
            self._necro_auto_last[key] = now
            self.write_local(f"[necroguard: {cmd}]", "#8888aa")
            self.send_line(cmd)

    def changeling_scan_line(self, clean):
        """Update changeling state from a text line: the status prompt (PP/ST
        carry Protoplasm=gp1 / Stamina=gp2, which the FFF feed mis-delivers -
        see changeling.py + the gp1/gp2 drop in mip_vitals_composite) or the
        `forms` table (per-form familiarity, for attack-form selection). Only
        runs for changelings; no-op on any other line."""
        upd = changeling.parse_prompt(clean)
        if upd:
            self.vitals.update(upd)
            for f in ("hp", "sp", "gp1", "gp2"):
                if f in upd and upd[f] > self.vitals_max.get(f, 0):
                    self.vitals_max[f] = upd[f]
                    self._maxes_dirty = True
            self.update_vitals()
            return
        rows = changeling.parse_forms_line(clean)
        if rows:
            for name, fam, nxt in rows:
                self.changeling_forms[name.lower()] = {
                    "name": name, "fam": fam, "next": nxt}
            return
        m = changeling.FORMS_POINTS.search(clean)
        if m:
            self.changeling_form_points = int(m.group(1))
            return
        bk = changeling.parse_best_kill(clean)
        if bk is not None:
            self.changeling_best_kill = bk

    def necro_scan_line(self, clean):
        """Update necro state from a text line: the status prompt (vitals +
        Status fields + auto-commands), the `powers` table block (bracketed
        by its header/footer so a power that drops out is zeroed), or a gs
        reagent line. Only runs for necromancers; only touches update_info
        when something relevant changed."""
        if necro.is_prompt(clean):
            self._necro_prompt(necro.parse_prompt(clean))
            return
        fired = necro.power_fired(clean)
        if fired is not None:
            cur = self.necro_counts.get(fired)
            if cur is not None and cur > 0:      # only when we know the count
                self.necro_counts[fired] = cur - 1
                if fired in self.tracked:
                    self.update_info()
            return
        inv = necro.parse_inv_line(clean)
        if inv:
            changed = False
            for name, val in inv.items():
                if self.necro_counts.get(name) != val:
                    self.necro_counts[name] = val
                    if name in self.tracked:
                        changed = True
            if changed:
                self.update_info()
            return
        changed = False
        if necro.POWERS_HEADER in clean:
            self._necro_powers_capture = {}
            return
        if self._necro_powers_capture is not None:
            if necro.POWERS_FOOTER in clean:
                cap = self._necro_powers_capture
                self._necro_powers_capture = None
                for name, low in self.tracked.items():
                    if name in necro.NECRO_POWERS:   # depleted -> absent -> 0
                        new = cap.get(name, 0)
                        if self.necro_counts.get(name) != new:
                            self.necro_counts[name] = new
                            changed = True
                self.necro_counts.update(cap)
                if changed:
                    self.update_info()
                return
            self._necro_powers_capture.update(necro.parse_powers_line(clean))
            return
        for name, val in necro.parse_reagents_line(clean).items():
            if self.necro_counts.get(name) != val:
                self.necro_counts[name] = val
                if name in self.tracked:
                    changed = True
        if changed:
            self.update_info()

    # ---- Bladesinger skill-GXP tracker (see blade.py) ------------------
    def blade_scan_line(self, clean):
        """Watch the `skills` readout (skill->cost rows + Total GXP) and the
        prompt G2N line. Keeps blade_skill_costs and a LIVE blade_available
        (spendable GXP) so a tracked skill can show GXP-still-needed."""
        refresh = False
        sk = blade.parse_skill_line(clean)
        if sk:
            name, cost = sk
            if self.blade_skill_costs.get(name) != cost:
                self.blade_skill_costs[name] = cost
                if name in self.tracked:
                    refresh = True
        g = blade.parse_g2n(clean)
        if g:
            avail = blade.available_gxp(*g)
            got = blade.glvl_from_g2n(*g)
            if got:
                self.blade_glvl = got[0]
            if avail is not None and avail != self.blade_available:
                self.blade_available = avail
                if self.tracked:
                    refresh = True
        else:
            # The "Total GXP:" box line is a fallback when no live G2N yet.
            tg = blade.parse_total_gxp(clean)
            if tg is not None and self.blade_available is None:
                self.blade_available = tg
                if self.tracked:
                    refresh = True
        if refresh:
            self.update_info()

    # ---- Viking GXP skill-cost capture (see viking.parse_vskills) -------
    def cmd_vskills(self, _arg=""):
        """`Vskills`: send `vskills` to the MUD and READ (not swallow) its
        output to refresh the Stats tab's per-skill training costs. Only the
        4 GXP pools + Daler stream live over MIP; the skill list updates only
        on demand here, so we don't scan it on every line."""
        if self.guild.lower() != "vikings":
            self.write_local("[vskills] only available on a Viking.",
                             "#cc9933")
            return
        self._vskills_capture = True
        self._vskills_lines = []
        self.send_line("vskills")
        self.write_local("[vskills] reading skill costs...", "#55aacc")

    def _vskills_finish(self):
        data = viking.parse_vskills(self._vskills_lines)
        self._vskills_capture = False
        self._vskills_lines = []
        if not data.get("trees"):
            self.write_local("[vskills] no skills parsed (unexpected "
                             "format?).", "#cc9933")
            return
        self.viking_skills = data
        n = sum(len(t["skills"]) for t in data["trees"])
        self.write_local(
            f"[vskills] loaded {n} skill(s) in {len(data['trees'])} "
            "tree(s) - see the Stats tab.", "#66cc66")
        if self.viking_win is not None and self.viking_win.winfo_exists():
            self.viking_win.set_vskills(data)

    def viking_scan_line(self, clean):
        """While a `Vskills` read is armed, accumulate the readout lines and
        finalize on the footer (or a safety line cap), then parse. Lines are
        still displayed normally - capture only reads them."""
        if not self._vskills_capture:
            return
        self._vskills_lines.append(clean)
        if "Trees:" in clean or len(self._vskills_lines) > 120:
            self._vskills_finish()

    # ---- Viking mission-board read + best-pick advisor (Missionlist) ----
    def cmd_missionlist(self, _arg=""):
        """`Missionlist`: send `vtrade stock` to get a trustworthy warehouse
        reading (the live WSTOCK/mip_trade_goods feed has been seen out of
        sync with actual stock), then `vmission list` to read the global
        mission board, then recommend which missions to accept for the
        highest total daler without exceeding stock or the remaining daily
        quota."""
        if self.guild.lower() != "vikings":
            self.write_local("Missionlist is a Vikings guild command.",
                             "#cc9933")
            return
        self._vtradestock_capture = True
        self._vtradestock_lines = []
        self.send_line("vtrade stock")
        self.write_local("[missionlist] reading warehouse stock...",
                         "#55aacc")

    def _vtradestock_finish(self):
        self._vtrade_stock = viking.parse_vtrade_stock(
            self._vtradestock_lines)
        self._vtradestock_capture = False
        self._vtradestock_lines = []
        self._missionlist_capture = True
        self._missionlist_lines = []
        self.send_line("vmission list")
        self.write_local("[missionlist] reading the mission board...",
                         "#55aacc")

    def _missionlist_finish(self):
        missions, used, maxq = viking.parse_mission_board(
            self._missionlist_lines)
        self._missionlist_capture = False
        self._missionlist_saw_board = False
        self._missionlist_lines = []
        if not missions:
            self.write_local("[missionlist] no missions parsed (unexpected "
                             "format?).", "#cc9933")
            return
        remaining = max(0, maxq - used)
        if remaining == 0:
            self._mission_picks = []
            self.update_info()
            self.write_local(
                f"[missionlist] quota {used}/{maxq} - none left until "
                "refresh.", "#cc9933")
            return
        stock = self._vtrade_stock
        chosen, daler, rep = viking.best_mission_picks(
            missions, stock, remaining)
        self._mission_picks = sorted(chosen, key=lambda m: -m["daler"])
        self.update_info()
        if not chosen:
            self.write_local(
                f"[missionlist] quota {used}/{maxq} ({remaining} left) - "
                "no mission is fully covered by current stock.", "#cc9933")
            return
        self.write_local(
            f"[missionlist] quota {used}/{maxq} ({remaining} left) - best "
            f"{len(chosen)}-mission pick: {daler} daler + {rep} rep "
            "- see the info pane.", "#66cc66")

    def _mission_fulfilled(self, mid):
        """A `vmission fulfill <id>` was just sent (see MISSION_FULFILL_RE
        in _raw_send) - drop that mission from the info-pane recommendation
        if it's one of ours."""
        before = len(self._mission_picks)
        self._mission_picks = [m for m in self._mission_picks
                               if m["id"] != mid]
        if len(self._mission_picks) != before:
            self.update_info()

    def _missionlist_scan_line(self, clean):
        """While a `Missionlist` read is armed, accumulate the `vtrade
        stock` readout first (finalizing on its footer Daler: line, then
        chaining into `vmission list`), then the `vmission list` readout
        (finalizing on its footer Quota: line), each with a safety line
        cap, then parse + recommend. Lines are still displayed normally -
        capture only reads them.

        `vmission list` (and `vmission newbie`) may now prepend the newbie
        errand board before the 'Global Mission Board' section. We ignore all
        lines until that banner appears so `_missionlist_lines` only ever
        contains the regular mission board - identical to the old output."""
        if self._vtradestock_capture:
            self._vtradestock_lines.append(clean)
            if "Daler:" in clean or len(self._vtradestock_lines) > 60:
                self._vtradestock_finish()
            return
        if not self._missionlist_capture:
            return
        if not self._missionlist_saw_board:
            if "Global Mission Board" in clean:
                self._missionlist_saw_board = True
                self._missionlist_lines.append(clean)
            return
        self._missionlist_lines.append(clean)
        if "Quota:" in clean or len(self._missionlist_lines) > 200:
            self._missionlist_saw_board = False
            self._missionlist_finish()

    # ---- Viking newbie-errand board read + best-pick advisor (VNlist) --
    def cmd_vnlist(self, arg):
        """`VNlist [metric]`: send `vmission newbie` and read the newbie
        errand board, then recommend the highest-`metric` errands up to
        the remaining daily quota. `metric` is 'daler' (default) or one of
        the trade goods newbie errands pay out in (see
        viking.NEWBIE_METRICS) - unlike Missionlist's paid missions,
        newbie errands cost no warehouse goods to accept, so there's
        nothing to conflict over and no stock to read first."""
        if self.guild.lower() != "vikings":
            self.write_local("VNlist is a Vikings guild command.",
                             "#cc9933")
            return
        metric = (arg.strip().lower() or "daler")
        if metric not in viking.NEWBIE_METRICS:
            self.write_local(
                f"[vnlist] unknown metric '{metric}' - one of "
                f"{', '.join(viking.NEWBIE_METRICS)}.", "#cc6666")
            return
        self._vnlist_metric = metric
        self._vnlist_capture = True
        self._vnlist_lines = []
        self.send_line("vmission newbie")
        self.write_local("[vnlist] reading the newbie errand board...",
                         "#55aacc")

    def _vnlist_finish(self):
        missions, used, maxq = viking.parse_newbie_board(self._vnlist_lines)
        self._vnlist_capture = False
        self._vnlist_lines = []
        if not missions:
            self.write_local("[vnlist] no newbie errands parsed "
                             "(unexpected format?).", "#cc9933")
            return
        remaining = max(0, maxq - used)
        if remaining == 0:
            self._newbie_picks = []
            self.update_info()
            self.write_local(
                f"[vnlist] quota {used}/{maxq} - none left until "
                "refresh.", "#cc9933")
            return
        metric = self._vnlist_metric
        chosen, total_metric, total_daler = viking.best_newbie_picks(
            missions, remaining, metric)
        self._newbie_picks = chosen
        self.update_info()
        if not chosen:
            self.write_local(f"[vnlist] quota {used}/{maxq} ({remaining} "
                             "left) - no newbie errands available.",
                             "#cc9933")
            return
        metric_txt = (f"{total_metric} daler" if metric == "daler" else
                      f"{total_metric} {metric} + {total_daler} daler")
        self.write_local(
            f"[vnlist] quota {used}/{maxq} ({remaining} left) - best "
            f"{len(chosen)}-errand pick by {metric}: {metric_txt} - see "
            "the info pane.", "#66cc66")

    def _newbie_fulfilled(self, mid):
        """A `vmission newbie accept <id>` was just sent (see
        NEWBIE_ACCEPT_RE in _raw_send) - drop that errand from the
        info-pane recommendation if it's one of ours."""
        before = len(self._newbie_picks)
        self._newbie_picks = [m for m in self._newbie_picks
                              if m["id"] != mid]
        if len(self._newbie_picks) != before:
            self.update_info()

    def _vnlist_scan_line(self, clean):
        """While a `VNlist` read is armed, accumulate the `vmission
        newbie` readout lines and finalize on the footer Quota: line (or
        a safety line cap - the newbie board runs much longer than the
        paid mission board), then parse + recommend. Lines are still
        displayed normally - capture only reads them."""
        if not self._vnlist_capture:
            return
        self._vnlist_lines.append(clean)
        if "Quota:" in clean or len(self._vnlist_lines) > 1000:
            self._vnlist_finish()

    def cmd_mob(self, arg):
        """`#mobs` -> DB count; `#mob <name>` -> matching mobs (name LIKE),
        one summary line each."""
        if self.mapdb is None:
            self.write_local("[mob] sqlite map backend not loaded.",
                             "#cc6666")
            return
        q = arg.strip()
        if not q:
            self.write_local(f"[mob] {self.mapdb.mob_count()} mobs in the "
                             "database. #mob <name> to search.", "#55aacc")
            return
        rows = self.mapdb.mobs_by_name(q)
        if not rows:
            self.write_local(f"[mob] no match for '{q}'.", "#cc9933")
            return
        self.write_local(f"[mob] {len(rows)} match '{q}':")
        for r in rows[:25]:
            cls = r["class"]
            self.write_local(
                f"  {r['name']}  @ {r['area_name'] or '?'}"
                + (f"  {r['race']}" if r["race"] else "")
                + (f"  class {cls:,}" if cls is not None else "")
                + (f"  aggr" if r["aggressive"] else ""))
        if len(rows) > 25:
            self.write_local(f"  ... and {len(rows) - 25} more")

    # ---- Mob database (vscan1 capture, see mobdb.py) -------------------
    def vscan_scan_line(self, clean):
        """Accumulate a vscan1 `[[ ... ]]` box and finalize it into the mob
        DB when it ends (the Miscellaneous 'Peaceable:' line). Non-box lines
        clear the buffer, so help `[[ ... ]]` boxes (no Peaceable) are
        dropped harmlessly."""
        inner = mobdb.box_inner(clean)
        if inner is None:
            self._vscan_buf = []
            return
        self._vscan_buf.append(clean)
        if len(self._vscan_buf) > 80:            # runaway guard
            self._vscan_buf = self._vscan_buf[-80:]
        if inner.startswith("Peaceable:"):
            buf, self._vscan_buf = self._vscan_buf, []
            self._vscan_finalize(buf)

    def _vscan_finalize(self, box):
        mob = mobdb.parse_vscan(box)
        if not mob:                              # not a mob box (e.g. help)
            return
        if self.mapdb is None:
            return
        area_id, area_name, coder = None, "", ""
        v = self._sql_cur_vnum
        if v is not None:
            row = self.mapdb.get_room(v)
            if row is not None and row["area_id"] is not None:
                area_id = row["area_id"]
                a = self.mapdb.conn.execute(
                    "SELECT name, author FROM areas WHERE area_id=?",
                    (area_id,)).fetchone()
                if a:
                    area_name, coder = a["name"], (a["author"] or "")
        if not area_name:
            self.write_local(
                f"[mob] '{mob['name']}' scanned but area unknown - rate the "
                "room (move/#map rate) then re-scan to file it.", "#cc9933")
            return
        added = self.mapdb.upsert_mob(
            mob, area_id, area_name, self.character,
            time.strftime("%Y-%m-%d %H:%M:%S"))
        self.write_local(
            f"[mob {'added' if added else 'updated'}: {mob['name']} @ "
            f"{area_name}"
            + (f" [{coder}]" if coder else "")
            + f"  (db {self.mapdb.mob_count()})]", "#66cc66")

    # --- field self-diagnosis (spec 5.1) ---
    def check_required_fields(self):
        """Guild layers declare required statline fields, e.g.
          "mip_required": [{"field": "blur_portal", "within": 30,
                            "hint": "set hpbar format to include
                                     B:/P:"}]
        If the field never appears within N seconds of combat
        activity, raise a ONE-TIME warning in the info pane."""
        reqs = self.cascade.get("mip_required", []) or []
        if not reqs:
            return
        now = time.time()
        if not hasattr(self, "_combat_seen_at"):
            self._combat_seen_at = now
        for r in reqs:
            field = r.get("field", "")
            within = r.get("within", 30)
            if not field or field in self._warned_fields:
                continue
            have = bool(getattr(self, field, None)) or \
                field in self.vars or field in self.vitals
            if have:
                self._warned_fields.add(field)   # satisfied: stop
                continue
            if now - self._combat_seen_at > within:
                hint = r.get("hint", "check mud-side hpbar settings")
                self.status_note(field,
                                 f"MIP field '{field}' never seen - "
                                 f"{hint}")

    # ------------------------------------------------ 1-second tick
    @staticmethod
    def fmt_mmss(seconds):
        seconds = int(seconds)
        if seconds >= 3600:
            return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:" \
                   f"{seconds % 60:02d}"
        return f"{seconds // 60}:{seconds % 60:02d}"

    # ------------------------------------------------ reminders
    # A persistent, cross-mud/cross-character timer. Reminders live in a
    # single shared reminders.json (see paths.REMINDERS_FILE); set one on
    # any character and it fires in whatever client is open when it comes
    # due - surviving logins, character switches and relaunches. Each tick
    # claims due reminders atomically (update_json re-reads fresh, so only
    # the one client that actually removes a reminder announces it; no
    # double-fire across simultaneously-open clients).
    _DUR_RE = re.compile(r"(\d+)\s*([smh])", re.I)

    @staticmethod
    def _parse_duration(text):
        """'5m', '30s', '2h', '1h30m', or a bare integer (minutes) ->
        seconds. Returns (seconds, rest_of_text) or (None, None)."""
        text = text.strip()
        m = re.match(r"(\d+)(?![smh\d:])", text)
        if m:                                  # bare number = minutes
            return int(m.group(1)) * 60, text[m.end():].strip()
        total, i, mult = 0, 0, {"s": 1, "m": 60, "h": 3600}
        for tok in MudClient._DUR_RE.finditer(text):
            if tok.start() != i:               # non-duration char hit
                break
            total += int(tok.group(1)) * mult[tok.group(2).lower()]
            i = tok.end()
        if total <= 0:
            return None, None
        return total, text[i:].strip()

    @staticmethod
    def _fmt_dur(secs):
        secs = int(round(secs))
        if secs < 0:
            secs = 0
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    def _reminder_cmd(self, arg, list_only=False):
        arg = arg.strip()
        if list_only or not arg or arg.lower() == "list":
            self._reminder_list()
            return
        low = arg.lower()
        if low == "clear" or low == "clear all" or low == "clearall":
            n = [0]

            def wipe(d):
                n[0] = len(d.get("reminders", []))
                d["reminders"] = []
            paths.update_json(paths.REMINDERS_FILE, wipe,
                              {"reminders": [], "next_id": 1})
            self._reminders_mtime = None
            self.write_local(f"[reminder] cleared {n[0]} reminder(s).",
                             "#ffcc44")
            return
        m = re.match(r"(?:clear|del|delete|cancel|rm)\s+(\d+)$", low)
        if m:
            rid = int(m.group(1))
            hit = [False]

            def drop(d):
                keep = [r for r in d.get("reminders", [])
                        if r.get("id") != rid]
                hit[0] = len(keep) != len(d.get("reminders", []))
                d["reminders"] = keep
            paths.update_json(paths.REMINDERS_FILE, drop,
                              {"reminders": [], "next_id": 1})
            self._reminders_mtime = None
            self.write_local(
                f"[reminder] {'removed' if hit[0] else 'no'} #{rid}.",
                "#ffcc44")
            return

        repeat = 0
        if low.startswith("every "):
            arg = arg[6:].strip()
        secs, text = self._parse_duration(arg)
        if secs is None:
            self.write_local(
                "Usage: Reminder <time> <text>   (e.g. Reminder 5m buff "
                "wore off, Reminder 1h30m reboot soon).  "
                "Reminder every <time> <text> repeats.  "
                "Reminder = list, Reminder clear [<id>|all].", "#cc6666")
            return
        if low.startswith("every "):
            repeat = secs
        if not text:
            text = "(reminder)"
        due = time.time() + secs
        new = [None]

        def add(d):
            rid = d.get("next_id", 1)
            d["next_id"] = rid + 1
            rec = {"id": rid, "due": due, "text": text,
                   "set_by": self.character, "mud": self.mud}
            if repeat:
                rec["repeat"] = repeat
            d.setdefault("reminders", []).append(rec)
            new[0] = rid
        paths.update_json(paths.REMINDERS_FILE, add,
                          {"reminders": [], "next_id": 1})
        self._reminders_mtime = None
        rpt = f", repeating every {self._fmt_dur(secs)}" if repeat else ""
        self.write_local(
            f"[reminder #{new[0]}] in {self._fmt_dur(secs)}{rpt}: {text}",
            "#ffcc44")

    def _reminder_list(self):
        data, err = paths.load_json(paths.REMINDERS_FILE,
                                    {"reminders": [], "next_id": 1})
        if err:
            self.write_local(f"[reminder] {err}", "#cc6666")
            return
        rems = sorted(data.get("reminders", []),
                      key=lambda r: r.get("due", 0))
        if not rems:
            self.write_local("[reminder] none set.", "#ffcc44")
            return
        now = time.time()
        self.write_local("Reminders (shared across all characters):",
                         "#ffcc44")
        for r in rems:
            eta = self._fmt_dur(r.get("due", 0) - now)
            rpt = (f" (every {self._fmt_dur(r['repeat'])})"
                   if r.get("repeat") else "")
            by = r.get("set_by", "")
            tag = f" [{by}]" if by and by != self.character else ""
            self.write_local(
                f"  #{r.get('id')}  in {eta}{rpt}{tag}: "
                f"{r.get('text', '')}", "#ffcc44")

    def _poll_reminders(self, now):
        try:
            mtime = os.path.getmtime(paths.REMINDERS_FILE)
        except OSError:
            return                              # no reminders.json yet
        if mtime != self._reminders_mtime:
            data, err = paths.load_json(
                paths.REMINDERS_FILE, {"reminders": [], "next_id": 1})
            if err:
                return                          # leave a hand-edit alone
            self._reminders_cache = data.get("reminders", [])
            self._reminders_mtime = mtime
        if not any(r.get("due", 0) <= now for r in self._reminders_cache):
            return
        fired = []

        def claim(d):
            keep = []
            for r in d.get("reminders", []):
                if r.get("due", 0) <= now:
                    fired.append(r)
                    if r.get("repeat"):         # reschedule repeaters
                        nxt = dict(r)
                        nxt["due"] = now + r["repeat"]
                        keep.append(nxt)
                else:
                    keep.append(r)
            d["reminders"] = keep
        paths.update_json(paths.REMINDERS_FILE, claim,
                          {"reminders": [], "next_id": 1})
        # refresh the cache to the post-fire state and adopt our own write's
        # mtime - so the info-pane display drops fired (non-repeat) reminders
        # immediately instead of showing them until the next external change.
        data, err = paths.load_json(
            paths.REMINDERS_FILE, {"reminders": [], "next_id": 1})
        if not err:
            self._reminders_cache = data.get("reminders", [])
        try:
            self._reminders_mtime = os.path.getmtime(paths.REMINDERS_FILE)
        except OSError:
            self._reminders_mtime = None
        for r in fired:
            self._announce_reminder(r)

    def _announce_reminder(self, r):
        txt = r.get("text", "(reminder)")
        by = r.get("set_by", "")
        tag = f" [set on {by}]" if by and by != self.character else ""
        # Make it loud: blank line, a full bar of slashes, the message, the
        # bar again, then a trailing blank line.
        bar = "/" * 60
        self.write_local("", "#ffcc44")
        self.write_local(bar, "#ffcc44")
        self.write_local(f"REMINDER{tag}: {txt}", "#ffcc44")
        self.write_local(bar, "#ffcc44")
        self.write_local("", "#ffcc44")
        play_sound(self.setting("tell_sound", "beep"))

    def tick(self):
        self._poll_webcmd()
        now = time.time()
        idle = now - self.last_sent
        manual_idle = now - self.last_manual
        dm_limit = self.deadman_minutes * 60 \
            if self.deadman_minutes else 0

        if dm_limit and manual_idle > dm_limit and \
                not self.deadman_tripped:
            self.deadman_tripped = True
            self.write_local(
                f"*** DEADMAN TRIPPED ({self.deadman_minutes}m no "
                "keyboard) - outgoing traffic BLOCKED. Type anything "
                "to release. ***", "#ff5555")

        eta = self.reboot_eta
        close = eta not in ("", "?") and "day" not in eta
        self.tb["uptime"].configure(
            text=f"up {self.uptime}  reboot in {eta}",
            fg="#cc6666" if close else "#99aacc")
        self.tb["reboot"].configure(text="")
        ex = " ".join(self.exits) if self.exits else "-"
        self.tb["room"].configure(text=f"{self.room} [{ex}]")
        self.tb["rounds"].configure(
            text=f"rnd {self.rounds}" + (" *" if self.in_combat
                                         else ""))
        self.tb["idle"].configure(text=f"idle {self.fmt_mmss(idle)}")
        if not dm_limit:
            self.tb["deadman"].configure(text="DM off", fg="#777788")
        elif self.deadman_tripped:
            self.tb["deadman"].configure(text="DM TRIPPED",
                                         fg="#ff5555")
        else:
            self.tb["deadman"].configure(
                text=f"DM {self.fmt_mmss(dm_limit - manual_idle)}",
                fg="#66cc66")
        self._poll_reminders(now)
        if self._reminders_cache:        # keep the info-pane countdown live
            self.update_info()
        self._write_webstate(now)
        self.call_hook("on_tick")
        self.root.after(1000, self.tick)

    def _poll_webcmd(self):
        """Phone/web dashboard (spec: docs/superpowers/specs/
        2026-06-21-web-dashboard-design.md): a command submitted from the
        dashboard goes through the exact same path as a keystroke
        (on_enter, below) - last_manual reset, deadman released, then
        process_input(manual=True) - so it counts as attention the same
        way the PC keyboard does."""
        try:
            text = paths.claim_webcmd(self.profile_id)
        except Exception:
            return
        if not text:
            return
        self.last_manual = time.time()
        if self.deadman_tripped:
            self.deadman_tripped = False
            self.write_local("*** Deadman released ***", "#66cc66")
        try:
            self.process_input(text, manual=True)
        except Exception:
            # process_input is the most exception-prone call in this file
            # (aliases, speedwalks, triggers, MIP handlers on arbitrary
            # text) and this is the first line of tick(), which ends with
            # root.after(1000, self.tick) - an uncaught throw here would
            # propagate out of tick() and permanently stop the heartbeat
            # (deadman, reminders, status bar, webstate all freeze), which
            # is exactly the away-from-keyboard failure this feature exists
            # to prevent. The deadman is already released above - log and
            # surface it instead of silently losing the command.
            self._log_webcmd_failure(text)

    def _log_webcmd_failure(self, text):
        import traceback
        try:
            with open(paths.CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(f"katmud dashboard command error "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"profile={self.profile_id} text={text!r}\n")
                f.write(traceback.format_exc() + "\n")
        except OSError:
            pass
        self.write_local(
            f"!! Dashboard command failed: {text!r} (see logs/crash.log)",
            "#cc6666")

    def _write_webstate(self, now):
        """Snapshot this character's dashboard state to disk every tick.
        Wrapped in try/except: tick() ends with root.after(1000, self.tick)
        below, so an unhandled exception here would silently stop the
        deadman timer, reminders, and the status bar forever - far worse
        than one missed dashboard update."""
        dm_limit = self.deadman_minutes * 60 if self.deadman_minutes else 0
        try:
            paths.save_json(paths.webstate_file(self.profile_id), {
                "updated": now,
                "room": self.room,
                "idle_seconds": now - self.last_sent,
                "deadman_tripped": self.deadman_tripped,
                "deadman_seconds_left":
                    (dm_limit - (now - self.last_manual)) if dm_limit
                    else None,
                "vitals": dict(self.vitals),
                "daler": self.viking_state.get("DALER"),
                "output": list(self._web_output),
                "chats": list(self._web_chats),
                "tells": list(self._web_tells),
            })
        except Exception:
            pass

    # ------------------------------------------------ input
    def on_enter(self, _event):
        raw = self.entry.get()
        self.entry.delete(0, "end")
        self.last_manual = time.time()
        if self.deadman_tripped:
            self.deadman_tripped = False
            self.write_local("*** Deadman released ***", "#66cc66")
        if raw == "":
            if self.conn and self.conn.send(""):
                self.last_sent = time.time()
            return
        minc = self.setting("history_min_chars", 0)
        if raw.strip() and self.echo_on and len(raw) > minc:
            if raw in self.history:
                self.history.remove(raw)
            self.history.append(raw)
            self.history = self.history[-200:]
        self.history_pos = None
        self.process_input(raw, manual=True)

    def _history_match(self, start, step):
        """Index of the next history entry from `start` (exclusive), moving
        by `step` (-1 older / +1 newer), that starts with the active prefix,
        or None if there isn't one. An empty prefix matches everything (the
        original 'cycle all' behavior)."""
        pref = self.history_prefix or ""
        i = start + step
        while 0 <= i < len(self.history):
            if self.history[i].startswith(pref):
                return i
            i += step
        return None

    def history_up(self, _event):
        if not self.history:
            return "break"
        if self.history_pos is None:
            # Begin navigation: anchor on what's typed so far. Up then walks
            # back through only the commands that start with it.
            self.history_prefix = self.entry.get()
            start = len(self.history)
        else:
            start = self.history_pos
        idx = self._history_match(start, -1)
        if idx is not None:
            self.history_pos = idx
            self.entry.delete(0, "end")
            self.entry.insert(0, self.history[idx])
        return "break"

    def history_down(self, _event):
        if self.history_pos is None:
            return "break"
        idx = self._history_match(self.history_pos, +1)
        self.entry.delete(0, "end")
        if idx is None:
            # Past the newest match: drop back to the prefix the user typed
            # and leave history navigation.
            self.history_pos = None
            self.entry.insert(0, self.history_prefix or "")
        else:
            self.history_pos = idx
            self.entry.insert(0, self.history[idx])
        return "break"

    def split_commands(self, line):
        sep = self.setting("command_separator", ";")
        parts, buf, i = [], [], 0
        while i < len(line):
            if line[i] == sep:
                if i + 1 < len(line) and line[i + 1] == sep:
                    buf.append(sep); i += 2; continue
                parts.append("".join(buf)); buf = []; i += 1
            else:
                buf.append(line[i]); i += 1
        parts.append("".join(buf))
        return [p for p in (s.strip() for s in parts) if p or not parts]

    def process_input(self, raw, depth=0, manual=False, gated=True):
        # gated=False marks a reactive command (trigger / combat reflex) so
        # its sends bypass the charting gate and fire immediately - see
        # send_line. It threads through aliases/speedwalks issued by the
        # trigger so the whole expansion bypasses, not just the first step.
        if depth > 10:
            self.write_local("!! Alias recursion limit hit.", "#cc6666")
            return
        if manual and depth == 0:
            # A real keystroke breaks any trigger feedback loop: reset the
            # runaway guard so suppressed triggers can fire again.
            self._trig_run_pattern = None
            self._trig_run_count = 0
            self._trig_run_warned = False
        if raw.lstrip().startswith("#"):
            self.client_command(raw.strip())
            return
        sep = self.setting("command_separator", ";")
        stripped = raw.lstrip()
        # A leading separator means "the rest is one speedwalk" - true both
        # for a typed line (depth 0) and for an alias whose body IS a
        # speedwalk (e.g. tostaysafe -> ';ru3ede(path)...', depth 1). Without
        # firing here at depth>0, split_commands would eat the leading sep and
        # send the un-expanded blob raw to the MUD.
        if stripped.startswith(sep):
            steps, err = self.expand_speedwalk(stripped[len(sep):])
            if err:
                self.write_local(f"[speedwalk] {err}", "#cc6666")
                return
            if manual and self.walk:
                self.cancel_walk()
            for s in steps:
                self.send_line(s, manual=manual, gated=gated)
            self._sql_blast_reset()
            return
        if manual and self.walk and depth == 0:
            self.cancel_walk()
        for cmd in self.split_commands(raw):
            expanded = self.expand_alias(cmd, depth, manual, gated)
            if expanded is None:
                continue
            words = expanded.split()
            # Capitalized `Go` / `Landmark` are the client commands (no #
            # needed); the lowercase forms pass straight through to the
            # MUD, so a MUD `go home` exit never collides with a `home`
            # landmark.
            if words and (words[0] in (
                    "Go", "Landmark", "Mapfix", "Chart",
                    "Maproom", "Maprerate", "Mapnote",
                    "Maplink", "Mapunlink", "Mapcheck",
                    "Mapwipe", "Toggle", "Toggles",
                    "VN", "Track", "Untrack", "Bot",
                    "Hunt", "Scan", "Gswap", "Run",
                    "Reagents", "Explore", "Agent",
                    "Reminder", "Reminders", "Corpse",
                    "Vskills", "Missionlist", "VNlist",
                    "Chaossea")
                    # a guild "macros" entry (e.g. bladesinger Autoregen) -
                    # capitalized like every other client command, but the
                    # name comes from data, not this hardcoded list.
                    or (words[0][:1].isupper()
                        and words[0].lower() in self._macro_defs())):
                self.client_command("#" + expanded)
                continue
            # sqlite single-word movement: a charted special exit (mapfix
            # setup/command, e.g. numpad-7 = 'climb over logs') is sent as
            # its command sequence instead of the bare word. Location is
            # tracked from DDD (following mode) or charted behind the gate
            # (charting mode) - both handled in send_line / packet intake -
            # so there is nothing to predict or note here.
            if self.map_backend == "sqlite" and len(words) == 1:
                word = words[0].lower()
                seq, _to, wait = self._sql_move_plan(word)
                if wait and not self._sql_charting:
                    # Timed exit walked by hand (numpad/typed): send the
                    # setup/prereq now, then the move after the delay - so
                    # hitting 8 does 'say rutabega', waits 2s, then 'n'.
                    # (Go avoids wait>0 exits; charting's gate already paces
                    # moves, so only following-mode manual moves need this.)
                    for c in (seq[:-1] if seq else []):
                        self.send_line(c, manual=manual, gated=gated)
                    move = seq[-1] if seq else word
                    self.root.after(
                        int(wait * 1000),
                        lambda m=move, mn=manual, g=gated:
                            self.send_line(m, manual=mn, gated=g))
                    self.write_local(f"[timed exit: '{move}' in {wait}s]",
                                     "#88aacc")
                    continue
                if seq is not None:
                    for c in seq:
                        self.send_line(c, manual=manual, gated=gated)
                    continue
            hooked = self.call_hook("on_command", expanded)
            if hooked is False:
                continue
            if isinstance(hooked, str):
                expanded = hooked
            self.send_line(expanded, manual=manual, gated=gated)

    def expand_alias(self, cmd, depth, manual, gated=True):
        words = cmd.split()
        if not words:
            return cmd
        template = self.aliases.get(words[0])
        if template is None:
            return cmd
        args = words[1:]
        out = template.replace("%*", " ".join(args))
        for n in range(1, 10):
            out = out.replace(f"%{n}",
                              args[n - 1] if n - 1 < len(args) else "")
        self.process_input(out, depth + 1, manual=manual, gated=gated)
        return None

    # ------------------------------------------------ speedwalk
    def expand_speedwalk(self, body):
        """Portal-style speedwalk: the WHOLE line after a leading
        separator. Grammar: [count]letter | [count](literal cmd).
        Letter meanings come from the cascading 'speedwalk' section
        (global.json defaults: nsewud + t=ne v=se z=sw q=nw r=enter
        o=out p=portal l=leave). ';15el8esr(open gate)q' works.
        Returns (commands, None) or (None, error_string)."""
        swmap = {k: v for k, v in
                 (self.cascade.get("speedwalk", {}) or {}).items()
                 if not k.startswith("_") and len(k) == 1}
        out, i, n = [], 0, len(body)
        while i < n:
            ch = body[i]
            if ch.isspace():
                i += 1
                continue
            count = 0
            while i < n and body[i].isdigit():
                count = count * 10 + int(body[i])
                i += 1
            count = min(count, 99) or 1
            if i >= n:
                break                    # trailing bare count: ignore
            ch = body[i]
            if ch == "(":
                j = body.find(")", i + 1)
                if j < 0:
                    return None, "unclosed '(' in speedwalk"
                cmd = body[i + 1:j].strip()
                i = j + 1
            else:
                cmd = swmap.get(ch.lower())
                if cmd is None:
                    return None, (f"'{ch}' not in speedwalk map - "
                                  "add it to the 'speedwalk' section "
                                  "or wrap the command in ()")
                i += 1
            if cmd:
                out.extend([cmd] * count)
        return out, None

    def send_line(self, cmd, manual=False, gated=True):
        if self.deadman_tripped and not manual:
            return
        # CHARTING mode: hold every outgoing command in the gate buffer;
        # _sql_pump releases them one at a time, each waiting for the room
        # packet before the next is sent, so the player can never outrun
        # the map building. Following mode sends straight through.
        #
        # gated=False bypasses the buffer and sends NOW. Reactive commands -
        # a trigger firing on a mob's attack/death, a guild combat reflex -
        # must not wait behind queued exploration moves (the gate also HOLDS
        # during combat, so a gated reaction could stall until the fight
        # ends - exactly the pain when charting through aggro mobs). The
        # gate only exists to pace DELIBERATE player/auto-walker movement,
        # which a reaction isn't. (A reaction that happens to move us, e.g.
        # flee, may leave one edge uncharted; following mode re-pins off the
        # next DDD, and an emergency flee beats a stalled heal.)
        if gated and self.map_backend == "sqlite" and self._sql_charting:
            self._sql_chart_dbg(f"buffered '{cmd.strip()}' "
                                f"(queue now {len(self._sql_chart_buf) + 1}, "
                                f"gate {'open' if self._sql_gate_open else 'SHUT'})")
            self._sql_chart_buf.append((cmd, manual))
            self._sql_pump()
            return
        if self.map_backend == "sqlite" and self._sql_charting:
            # charting on but this send bypassed the gate (gated=False, or a
            # reactive path) - it will NOT chart an edge. Visible so a move
            # that silently skips the gate is obvious.
            self._sql_chart_dbg(f"BYPASS gate (ungated) '{cmd.strip()}' "
                                "- no edge will be charted")
        self._raw_send(cmd, manual)

    def _raw_send(self, cmd, manual=False):
        if self.echo_on:
            self.write_local("> " + cmd, "#777788")
        if self._mission_picks:
            m = MISSION_FULFILL_RE.match(cmd)
            if m:
                self._mission_fulfilled(int(m.group(1)))
        if self._newbie_picks:
            m = NEWBIE_ACCEPT_RE.match(cmd)
            if m:
                self._newbie_fulfilled(int(m.group(1)))
        if self.conn and self.conn.send(cmd):
            self._last_move = cmd.strip()
            self.last_sent = time.time()
            if manual:
                self.last_manual = time.time()
        else:
            self.write_local("Not connected. #connect to retry.",
                             "#cc6666")

    # ------------------------------------------------ gags & combat
    def is_gagged(self, clean):
        for _p, rx in self.gags:
            if rx.search(clean):
                return True
        return False

    def chat_is_gagged(self, line):
        """Filter for the chat/tell pane only (party divvy spam, etc.).
        Tested against the composed display line, so a pattern can match
        the channel tag too, e.g. /\\[PARTY\\] Divvy of/."""
        for _p, rx in self.chat_gags:
            if rx.search(line):
                return True
        return False

    def track_combat_line(self, clean):
        m = DAMAGE_RE.match(clean)
        if m:
            hits = int(m.group(2))
            dmg = int(m.group(3).replace(",", ""))
            bucket = ("portals" if self.vars.get("portals_active")
                      else "noportals")
            d = self.dmg[bucket]
            d["rounds"] += 1
            d["hits"] += hits
            d["damage"] += dmg
            if not self._fff_combat:
                self.rounds += 1
                self.in_combat = True
        for _p, rx in self.kill_res:
            km = rx.search(clean)
            if km:
                mob = (km.group(km.re.groups) if km.re.groups
                       else "?").strip()
                self.kills.append((mob, self.rounds))
                self.kills = self.kills[-8:]   # newest 8; oldest rolls off
                if self._run_on:
                    self._run_kills += 1
                self.rounds = 0
                self.in_combat = False
                # Clear the cached enemy name now too, not just in_combat -
                # otherwise a stray post-kill round tick (e.g. a guild's
                # kill-trigger chain casting an ability) can see the still-
                # stale enemy field and re-arm in_combat before the FFF K
                # packet that would normally clear it ever arrives.
                self.vitals["enemy"] = ""
                self.vitals.pop("enemycond", None)
                self.update_info()
                if self._maxes_dirty:
                    self.persist_seen_max()
                self._fire_corpse(mob)
                break

    # ---- corpse (on-kill loot) handling --------------------------------
    def _fire_corpse(self, mob=None):
        """Run the on-kill loot routine: the `party` command when #party is
        on (falling back to `solo` if no party-specific one is set), else
        `solo`. Optionally gated by a Toggle switch (cfg `toggle`, e.g.
        'harvest'). Sent reactively (gated=False) so it bypasses the
        charting gate. %m in the command expands to the victim's name."""
        cfg = self.corpse_cfg
        if not cfg:
            return
        tog = cfg.get("toggle")
        if tog and not self.trigger_toggles.get(tog, True):
            return                              # routine switched off
        party = self.party_on()
        cmd = cfg.get("party") if party else cfg.get("solo")
        if cmd is None and party:               # no party-specific command:
            cmd = cfg.get("solo")               # reuse solo in a party too
        if not cmd:
            return
        if mob and "%m" in cmd:
            cmd = cmd.replace("%m", mob)
        self.process_input(cmd, gated=False)

    def cmd_corpse(self, arg):
        """`Corpse` / `#corpse` - view or edit the on-kill loot routine.
          Corpse                       show solo/party commands + source
          Corpse <cmds>  | Corpse solo <cmds>   set the solo routine
          Corpse party <cmds>                   set the party routine
          Corpse off | solo off | party off     clear it
        Separate multiple commands with '/' (NOT the ';' command separator,
        which would split the line before this command sees it): e.g.
        `Corpse bury corpse/glance` is stored as `bury corpse;glance`.
        Saved to the guild layer (corpse routines are guild-specific); if
        the character has no guild, falls back to the character layer."""
        arg = arg.strip()
        scope = "guild" if (self.guild and self.guild.lower() != "none") \
            else "character"
        low = arg.lower()
        if not arg:
            cfg = self.corpse_cfg
            if not cfg:
                self.write_local(
                    "No corpse routine set. Set one with '/' between "
                    "commands (the ';' separator can't be typed here): e.g. "
                    "Corpse bury corpse/glance. Corpse party <cmds> for a "
                    "party-mode variant.", "#cc9933")
                return
            self.write_local("Corpse routine (on kill):", "#cc99ee")
            for key in ("solo", "party"):
                val = cfg.get(key)
                if val is None and key == "party":
                    self.write_local("  party   (uses solo)")
                    continue
                src = self.cascade.source_of("corpse", key) or "?"
                self.write_local(f"  {key:<7} ({src}) {val!r}")
            tog = cfg.get("toggle")
            if tog:
                state = "on" if self.trigger_toggles.get(tog, True) else "off"
                self.write_local(f"  gated by Toggle {tog} (now {state})")
            self.write_local(f"  fires in {'PARTY' if self.party_on() else 'SOLO'}"
                             " mode right now.")
            return
        # which key?
        key = "solo"
        m = re.match(r"(solo|party)\b\s*(.*)$", arg, re.I)
        if m:
            key = m.group(1).lower()
            arg = m.group(2).strip()
            low = arg.lower()
        if low in ("", "off", "clear", "none"):
            err = self.cascade.delete_entry(scope, "corpse", key)
            if err:
                self.write_local(f"[corpse] {err}", "#cc6666")
                return
            self.load_layers()
            self.write_local(f"[corpse] {key} routine cleared "
                             f"({self.cascade.label_for(scope)}).", "#cc99ee")
            return
        # '/' is the in-game separator for this command (';' would have been
        # eaten by split_commands before we got here); store it as the real
        # command separator so the routine runs as multiple commands.
        sep = self.setting("command_separator", ";")
        value = arg.replace("/", sep)
        err = self.cascade.save_entry(scope, "corpse", key, value)
        if err:
            self.write_local(f"[corpse] {err}", "#cc6666")
            return
        self.load_layers()
        self.write_local(f"[corpse] {key} routine set "
                         f"({self.cascade.label_for(scope)}): {value}",
                         "#cc99ee")

    def persist_seen_max(self):
        vm = dict(self.vitals_max)

        def mutate(data):
            data.setdefault("seen_max", {}).update(vm)
        paths.update_json(paths.character_file(self.character), mutate,
                          default=config.CHARACTER_TEMPLATE)
        self._maxes_dirty = False

    DELTA_RE = re.compile(
        r"\[\s*(-?\d+)\s*/\s*(-?\d+)\s*/\s*(-?\d+)\s*/?\s*(-?\d+)\s*\]")

    def parse_deltas(self, stripped):
        m = self.DELTA_RE.search(stripped)
        if not m:
            return stripped
        hp, sp, g1, g2 = (int(x) for x in m.groups())
        return f"\u0394 hp {hp:+d}  sp {sp:+d}  gp {g1:+d}/{g2:+d}"

    def update_info(self):
        lines = []
        if self.room_vnum is not None:
            lines.append(f"vnum: {self.room_vnum}")
        if self.show_mapdetail:
            lines.extend(self._mapdetail_lines())
        if self.guild.lower() == "changelings":
            lines.append(f"bioplasts: {changeling.bioplasts(self.vitals)}")
        if self.last_deltas:
            lines.append(self.last_deltas)
        if self.blur_portal:
            lines.append(self.blur_portal)
        if self.guild.lower() == "gentech":
            for f in ("hpbar1", "hpbar2"):
                txt = strip_mip_colors(str(self.vitals.get(f, ""))).strip()
                if txt:
                    lines.append(txt)
        lines.extend(self._merc_lines())
        if self.kills:
            lines.append("recent kills (rounds):")
            for mob, rnds in reversed(self.kills):
                lines.append(f"  {mob} ({rnds})")
        if self.last_unknown_mip:
            lines.append(f"mip? {self.last_unknown_mip}")
        for note in self.status_notes:
            lines.append(f"! {note}")
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("end", "\n".join(lines))
        self._render_necro_status()
        self._render_tracked()
        self._render_reminders()
        self._render_missions()
        self._render_newbie_missions()
        self.info.configure(state="disabled")

    def _render_necro_status(self):
        """Necro Status block in the info pane: worth / protection / veil /
        reset / circle, with worth<125 and protection/veil OFF flagged red
        (the conditions the necroguard auto-commands act on)."""
        if self.guild.lower() != "necromancers":
            return
        v = self.vars
        if not any(k in v for k in
                   ("worth", "protection", "veil", "reset", "circle")):
            return
        if self.info.index("end-1c") != "1.0":
            self.info.insert("end", "\n")
        self.info.insert("end", "Necro\n", "track_hdr")
        if "worth" in v:
            self.info.insert("end", "  Worth: ")
            self.info.insert("end", f"{v['worth']}%\n",
                             "necro_bad" if v["worth"] < 125 else "necro_ok")
        if "protection" in v or "veil" in v:
            self.info.insert("end", "  ")
            for key, label in (("protection", "Prot"), ("veil", "Veil")):
                if key in v:
                    self.info.insert("end", f"{label} ")
                    self.info.insert("end", "ON  " if v[key] else "OFF  ",
                                     "necro_ok" if v[key] else "necro_bad")
            self.info.insert("end", "\n")
        if "reset" in v:
            self.info.insert("end", f"  Reset: {v['reset']}%\n")
        if "circle" in v:
            self.info.insert("end", f"  Circle: {v['circle']}\n")
            tp = v.get("tier")
            if tp:
                self.info.insert(
                    "end", f"  Tier: {tp['idx'] + 1}/{tp['total']}  "
                    f"({tp['pct']}%)\n", "necro_ok")

    def _render_tracked(self):
        """Append the Track section to the info pane: one line per tracked
        name (`name: value`), highlighted dim-red when value < its low
        threshold so a depleting power/reagent stands out. Called inside
        update_info's normal/disabled window."""
        if not self.tracked:
            return
        if self.info.index("end-1c") != "1.0":
            self.info.insert("end", "\n")
        self.info.insert("end", "Tracking\n", "track_hdr")
        if self.guild.lower() == "bladesingers":
            self._render_blade_tracked()
            return
        for name in sorted(self.tracked):
            low = self.tracked[name]
            val = self.necro_counts.get(name)
            shown = "?" if val is None else str(val)
            suffix = f"  (<{low})" if low is not None else ""
            line = f"  {name}: {shown}{suffix}\n"
            low_hit = (low is not None and val is not None and val < low)
            self.info.insert("end", line, "track_low" if low_hit else ())

    def _render_blade_tracked(self):
        """Tracked Bladesinger skills: GXP still needed to raise each one
        (skill_cost - live spendable GXP). READY (green) once affordable;
        maxed skills (N/A cost) and unknown costs are noted plainly."""
        avail = self.blade_available
        if avail is not None:
            tag = " " * 2
            self.info.insert("end", f"{tag}(have {blade.fmt_gxp(avail)}"
                             + (f", glvl {self.blade_glvl}"
                                if self.blade_glvl else "") + ")\n")
        for name in sorted(self.tracked):
            if name not in self.blade_skill_costs:
                self.info.insert("end", f"  {name}: ? (read `skills`)\n")
                continue
            cost = self.blade_skill_costs[name]
            if cost is None:
                self.info.insert("end", f"  {name}: maxed\n", "necro_ok")
                continue
            if avail is None:
                self.info.insert("end",
                                 f"  {name}: cost {blade.fmt_gxp(cost)}\n")
                continue
            need = cost - avail
            if need <= 0:
                self.info.insert("end", f"  {name}: READY "
                                 f"({blade.fmt_gxp(cost)})\n", "necro_ok")
            else:
                self.info.insert("end", f"  {name}: need "
                                 f"{blade.fmt_gxp(need)} "
                                 f"({blade.fmt_gxp(cost)})\n", "necro_bad")

    def _render_reminders(self):
        """Append a Reminders section to the info pane - the shared reminders
        (all characters), counting down. Shown on EVERY character by default,
        guild-independent, so a pending Reminder is always in view. The tick
        re-renders while any reminder is pending so the ETA stays live."""
        rems = sorted(self._reminders_cache, key=lambda r: r.get("due", 0))
        if not rems:
            return
        if self.info.index("end-1c") != "1.0":
            self.info.insert("end", "\n")
        self.info.insert("end", "Reminders\n", "track_hdr")
        now = time.time()
        for r in rems:
            left = r.get("due", 0) - now
            rpt = " (rpt)" if r.get("repeat") else ""
            by = r.get("set_by", "")
            tag = f" [{by}]" if by and by != self.character else ""
            text = r.get("text", "")
            if len(text) > 22:
                text = text[:21] + "…"
            line = f"  {self._fmt_dur(left)}{rpt}{tag}: {text}\n"
            self.info.insert("end", line, "track_low" if left <= 0 else ())

    def _render_missions(self):
        """Append a Missions section to the info pane: the Missionlist
        recommendation, one line per mission still to run. A pick clears
        itself when fulfilled - see MISSION_FULFILL_RE in _raw_send."""
        if not self._mission_picks:
            return
        if self.info.index("end-1c") != "1.0":
            self.info.insert("end", "\n")
        self.info.insert("end", "Missions\n", "track_hdr")
        for m in self._mission_picks:
            need = ", ".join(f"{q} {g}" for q, g in m["requirements"])
            self.info.insert(
                "end",
                f"  [{m['id']}] {m['town']}: {need} -> {m['daler']}d\n")

    def _render_newbie_missions(self):
        """Append a Newbie Errands section to the info pane: the VNlist
        recommendation, one line per errand still to run (fetch_town ->
        town matches VN's <start> <destination> order). A pick clears
        itself when accepted - see NEWBIE_ACCEPT_RE in _raw_send."""
        if not self._newbie_picks:
            return
        if self.info.index("end-1c") != "1.0":
            self.info.insert("end", "\n")
        self.info.insert("end", "Newbie Errands\n", "track_hdr")
        for m in self._newbie_picks:
            extra = ", ".join(f"{q} {g}" for q, g in m["goods"])
            if extra:
                extra = ", " + extra
            self.info.insert(
                "end",
                f"  [{m['id']}] {m['fetch_town']} -> {m['town']}: "
                f"{m['daler']}d{extra}, {m['rep_min']}-{m['rep_max']}rep\n")

    def _merc_lines(self):
        """Mercenary status block for the info pane (see mip_mercenary)."""
        m = self.merc_state
        if not m or not m.get("name"):
            return []
        lines = [f"Mercenary: {m['name']}"]
        if "hp" in m and "hpmax" in m:
            lines.append(
                f"  HP {m['hp']}/{m['hpmax']}  "
                f"Stam {m.get('stamina', '?')}/{m.get('staminamax', '?')}  "
                f"AP {m.get('ap', '?')}/{m.get('apmax', '?')}")
        if "perm_level" in m:
            lines.append(
                f"  Lvl {m['perm_level']} "
                f"({m.get('perm_xp', 0)}/{m.get('perm_xp_next', '?')} xp)  "
                f"Inst {m.get('inst_level', '?')} "
                f"({m.get('inst_xp', 0)}/{m.get('inst_xp_next', '?')})")
        if m.get("target_name"):
            lines.append(
                f"  Target: {m['target_name']} "
                f"({m.get('target_hp_pct', '?')}%)")
        extra = []
        if m.get("damage_type"):
            extra.append(m["damage_type"])
        extra.append("following" if m["following"] else "not following")
        if "cost_per_round" in m:
            extra.append(f"{m['cost_per_round']}/rd")
        lines.append("  " + ", ".join(extra))
        if "funds" in m:
            lines.append(
                f"  Funds: {m['funds']}  Spent: {m.get('spent', 0)}")
        if m.get("abilities"):
            lines.append(f"  {m['abilities']}")
        return lines

    def _mapdetail_lines(self):
        """The current room's exits + notes for the info pane (sqlite
        backend, `Toggle mapdetail` on). Cheap reads off the open db."""
        if self.map_backend != "sqlite" or self.mapdb is None:
            return []
        v = self._sql_cur_vnum
        if v is None:
            return []
        row = self.mapdb.get_room(v)
        if row is None:
            return ["(uncharted room)"]
        out = []
        lms = sorted({r["tag"] for r in self.mapdb.landmarks_for()
                      if r["vnum"] == v})
        if lms:
            out.append("landmark: " + ", ".join(lms))
        out.append("exits:")
        ex = sorted(self.mapdb.iter_exits(v), key=lambda r: r["direction"])
        if not ex:
            out.append("  (none)")
        for e in ex:
            dest = e["to_vnum"]
            cmd = f" ({e['command']})" if e["command"] else ""
            out.append(
                f"  {e['direction']:<6}"
                f"{('-> ' + str(dest)) if dest is not None else '-> ?'}"
                f"{cmd}")
            pre = []
            if e["setup_command"]:           # prerequisite (unlock/passcode)
                pre.append(f"setup: {e['setup_command']}")
            if e["wait_seconds"]:
                pre.append(f"wait {e['wait_seconds']}s")
            if pre:
                out.append("        " + "; ".join(pre))
        if row["notes"]:
            out.append("notes:")
            for ln in row["notes"].splitlines():
                out.append(f"  {ln}")
        return out

    # ------------------------------------------------ triggers
    def party_on(self):
        return str(self.vars.get("party", "off")).lower() in (
            "on", "1", "true", "yes")

    def trigger_enabled(self, pattern):
        """A trigger tagged with `toggle` fires only while that switch is on
        (default on); untagged triggers always fire."""
        name = self.trigger_toggle_of.get(pattern)
        return name is None or self.trigger_toggles.get(name, True)

    def check_triggers(self, clean):
        if self._trig_tripped:           # flood breaker latched - all off
            return
        party = self.party_on()
        now = time.time()
        for pattern, compiled, command, sound, cooldown in self.triggers:
            mode = self.trigger_modes.get(pattern)
            if (mode == "party" and not party) or \
               (mode == "noparty" and party):
                continue
            if not self.trigger_enabled(pattern):
                continue
            m = compiled.search(clean)
            if not m:
                continue
            play_sound(sound)
            if not command:
                continue
            # Per-trigger cooldown (opt-in): this trigger can't refire within
            # `cooldown` secs - stops a trigger looping on its own echo.
            if cooldown and now - self._trig_cooldowns.get(pattern, 0) \
                    < cooldown:
                continue
            # A trigger may opt out of the guards via "recursion_limit" (for a
            # MUD-forced loop that must fire many times). rlim None = use the
            # global cap + count toward the flood breaker; rlim set = per-trigger
            # cap (0 = unlimited) AND exempt from the flood breaker.
            rlim = self.trigger_limits.get(pattern)
            exempt = rlim is not None
            # Runaway guard: count consecutive fires of this same pattern. A
            # different pattern (below) or a manual command (process_input)
            # resets the run. Past the limit we keep matching but stop running
            # the command, so a feedback loop can't flood the MUD.
            cap = rlim if exempt else TRIGGER_RECURSION_LIMIT
            if pattern == self._trig_run_pattern:
                self._trig_run_count += 1
            else:
                self._trig_run_pattern = pattern
                self._trig_run_count = 1
                self._trig_run_warned = False
            if cap and self._trig_run_count > cap:
                if not self._trig_run_warned:
                    self._trig_run_warned = True
                    self.write_local(
                        f"!! Trigger /{pattern}/ fired {cap} times in a row - "
                        "suppressing (runaway loop?). A manual command or a "
                        "different trigger resets it.", "#cc6666")
                continue
            # Global flood breaker: count ALL fires in a sliding window
            # (pattern-agnostic), so an ALTERNATING loop the consecutive guard
            # misses still trips. Past the limit, latch OFF until reload.
            # recursion_limit triggers are exempt (they don't count or trip).
            if not exempt:
                window = max(0.5,
                             float(self.setting("trigger_flood_window_s", 3.0)))
                limit = max(1, int(self.setting("trigger_flood_limit", 25)))
                self._trig_fires.append(now)
                while self._trig_fires and now - self._trig_fires[0] > window:
                    self._trig_fires.popleft()
                if len(self._trig_fires) > limit:
                    self._trig_tripped = True
                    self.write_local(
                        f"!! Trigger FLOOD: {len(self._trig_fires)} fires in "
                        f"{window:g}s (last /{pattern}/) - ALL trigger firing "
                        "PAUSED to stop MUD spam. Fix the trigger(s), then "
                        "#reload (or relaunch).", "#cc6666")
                    return
            self._trig_cooldowns[pattern] = now
            out = command
            for n in range(min(9, compiled.groups), 0, -1):
                out = out.replace(f"\\{n}", m.group(n) or "")
            self.write_local(f"[trigger {pattern}]", "#aa88cc")
            # gated=False: a trigger is a reaction (mob died/attacked) and
            # must fire NOW, not wait behind queued charting moves (and the
            # gate holds during combat - a gated reaction could stall till
            # the fight ends). not manual: deadman still applies.
            self.process_input(out, gated=False)

    # ------------------------------------------------ quick edits
    # #alias / #trigger / #gag write to the CHARACTER scope - the
    # narrowest default (spec 4). The builder dialog offers all scopes.
    def quick_save_alias(self, name, body):
        err = self.cascade.save_entry("character", "aliases", name,
                                      body)
        if err:
            self.write_local(f"save failed: {err}", "#cc6666")
        self.load_layers()

    def quick_delete_alias(self, name):
        scope = self.cascade.source_of("aliases", name)
        if scope is None:
            return False
        err = self.cascade.delete_entry(scope, "aliases", name)
        if err:
            self.write_local(f"delete failed: {err}", "#cc6666")
        self.load_layers()
        return True

    def quick_save_trigger(self, item):
        err = self.cascade.save_entry("character", "triggers", None,
                                      item)
        if err:
            self.write_local(f"save failed: {err}", "#cc6666")
        self.load_layers()

    def quick_delete_trigger(self, pattern):
        scope = self.cascade.source_of("triggers", pattern)
        if scope is None:
            return False
        err = self.cascade.delete_entry(scope, "triggers", pattern)
        if err:
            self.write_local(f"delete failed: {err}", "#cc6666")
        self.load_layers()
        return True

    def trigger_item(self, pattern):
        for t in self.cascade.get("triggers", []) or []:
            if t.get("pattern") == pattern:
                return dict(t)
        return None

    # ------------------------------------------------ client commands
    def client_command(self, cmd):
        parts = cmd.split(None, 1)
        name = parts[0][1:].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if name == "connect":
            if arg:
                bits = arg.split()
                self.host = bits[0]
                if len(bits) > 1:
                    self.port = int(bits[1])
            self.connect()
        elif name == "disconnect":
            self.disconnect()
        elif name == "reload":
            self.reload_cascade()
            self.load_scripts(announce=True)
        elif name in ("reminder", "reminders"):
            self._reminder_cmd(arg, list_only=(name == "reminders"))
        elif name == "vskills":
            self.cmd_vskills(arg)
        elif name == "missionlist":
            self.cmd_missionlist(arg)
        elif name == "vnlist":
            self.cmd_vnlist(arg)
        elif name == "corpse":
            self.cmd_corpse(arg)
        elif name == "mipraw":
            self.mipraw = not self.mipraw
            if self.mipraw:
                self.mipraw_buf = []
                self.write_local("MIP raw display ON (toggle off with "
                                 "#mipraw to save to log).", "#55aacc")
            else:
                fname = os.path.join(
                    paths.LOGS_DIR,
                    time.strftime("mip_%Y%m%d_%H%M%S.log"))
                try:
                    with open(fname, "w", encoding="utf-8") as f:
                        f.write("\n".join(self.mipraw_buf))
                    self.write_local(
                        f"MIP raw OFF - {len(self.mipraw_buf)} packets"
                        f" saved to {fname}", "#55aacc")
                except OSError as e:
                    self.write_local(f"MIP log save failed: {e}",
                                     "#cc6666")
        elif name == "combat":
            a = arg.strip().lower()
            if a in ("clear", "reset", "off"):
                was = self.in_combat
                self.in_combat = False
                self.write_local(
                    f"[combat] in_combat forced False (was {was}).",
                    "#cc66cc")
            else:
                self.write_local(
                    f"[combat] in_combat={self.in_combat} "
                    f"fff_combat={self._fff_combat} "
                    f"enemy={self.vitals.get('enemy', '')!r} "
                    f"run_on={self._run_on} "
                    f"run_killing={getattr(self, '_run_killing', None)} "
                    f"run_engaged={getattr(self, '_run_engaged', None)} "
                    "(#combat clear to force it off)", "#cc66cc")
        elif name == "hpbar":
            i_raw = self.vitals.get("hpbar1", "")
            j_raw = self.vitals.get("hpbar2", "")
            self.write_local(
                f"[hpbar] I raw={i_raw!r}\n"
                f"        I stripped={strip_mip_colors(i_raw).strip()!r}\n"
                f"        J raw={j_raw!r}\n"
                f"        J stripped={strip_mip_colors(j_raw).strip()!r}\n"
                f"        stat1={self.stat1!r} stat2={self.stat2!r} "
                f"blur_portal={self.blur_portal!r} "
                f"last_deltas={self.last_deltas!r}", "#cc66cc")
        elif name == "markers":
            self.markers_debug = not self.markers_debug
            if self.markers_debug:
                self.write_local(
                    "Marker debug ON - reporting italic (mob) / underline "
                    "(player) room lines. Needs 'aset look_monster italics' "
                    "and 'aset look_player underline' set on the MUD. "
                    "#markers to stop.", "#cc66cc")
            else:
                self.write_local("Marker debug OFF.", "#cc66cc")
        elif name == "chartdebug":
            self._sql_chart_debug = not self._sql_chart_debug
            self.write_local(
                "Chart gate trace " + ("ON - shows each pump/arrival/edge "
                "so wrong-room edges and stalls are visible. #chartdebug "
                "to stop." if self._sql_chart_debug else "OFF."), "#8899bb")
        elif name == "viking":
            self.open_viking_status()
        elif name == "gag":
            m = re.match(r"\s*/(.+)/\s*$", arg)
            if not m:
                self.write_local("Usage: #gag /pattern/   (toggles; "
                                 "matching lines are hidden)")
                return
            pattern = m.group(1)
            if any(g[0] == pattern for g in self.gags):
                scope = self.cascade.source_of("gags", pattern) \
                    or "character"
                self.cascade.delete_entry(scope, "gags", pattern)
                self.write_local(f"Gag /{pattern}/ removed.")
            else:
                try:
                    re.compile(pattern)
                except re.error as e:
                    self.write_local(f"Bad regex: {e}", "#cc6666")
                    return
                self.cascade.save_entry("character", "gags", pattern,
                                        pattern)
                self.write_local(f"Gag /{pattern}/ added.")
            self.load_layers()
        elif name == "gags":
            if not self.gags:
                self.write_local("No gags defined.")
            for p, _x in self.gags:
                scope = self.cascade.source_of("gags", p) or "?"
                self.write_local(f"  ({scope}) /{p}/")
        elif name == "chatgag":
            m = re.match(r"\s*/(.+)/\s*$", arg)
            if not m:
                self.write_local(
                    "Usage: #chatgag /pattern/   (toggles; matching "
                    "chat/tell lines are hidden from the chat pane)")
                return
            pattern = m.group(1)
            if any(g[0] == pattern for g in self.chat_gags):
                scope = self.cascade.source_of("chat_gags", pattern) \
                    or "character"
                self.cascade.delete_entry(scope, "chat_gags", pattern)
                self.write_local(f"Chat gag /{pattern}/ removed.")
            else:
                try:
                    re.compile(pattern)
                except re.error as e:
                    self.write_local(f"Bad regex: {e}", "#cc6666")
                    return
                self.cascade.save_entry("character", "chat_gags",
                                        pattern, pattern)
                self.write_local(f"Chat gag /{pattern}/ added.")
            self.load_layers()
        elif name == "chatgags":
            if not self.chat_gags:
                self.write_local("No chat gags defined.")
            for p, _x in self.chat_gags:
                scope = self.cascade.source_of("chat_gags", p) or "?"
                self.write_local(f"  ({scope}) /{p}/")
        elif name == "dmg":
            if arg.strip() == "reset":
                for d in self.dmg.values():
                    d.update(rounds=0, hits=0, damage=0)
                self.write_local("Damage tracker reset.")
                return
            o = {k: sum(d[k] for d in self.dmg.values())
                 for k in ("rounds", "hits", "damage")}
            rows = [("Overall", o),
                    ("noportals", self.dmg["noportals"]),
                    ("portals", self.dmg["portals"])]
            self.write_local(
                f"{'':12}{'Rounds':>10}{'Hits':>10}{'Damage':>15}"
                f"{'DPR':>9}{'DPH':>9}")
            for label, d in rows:
                dpr = d["damage"] / d["rounds"] if d["rounds"] else 0
                dph = d["damage"] / d["hits"] if d["hits"] else 0
                self.write_local(
                    f"{label:12}{d['rounds']:>10,}{d['hits']:>10,}"
                    f"{d['damage']:>15,}{dpr:>9,.0f}{dph:>9,.0f}")
        elif name == "party":
            a = arg.strip().lower()
            if a in ("on", "off"):
                self.vars["party"] = a
            elif a:
                self.write_local(
                    "Usage: #party [on|off]  (no arg toggles)")
                return
            else:
                self.vars["party"] = "off" if self.party_on() else "on"
            self.write_local(
                f"Party mode {self.vars['party'].upper()}.", "#aa88cc")
        elif name == "trigmode":
            m = re.match(r"\s*/(.+?)/\s*(?:=\s*(\w+))?$", arg)
            if not m:
                self.write_local(
                    "Usage: #trigmode /regex/ = party|noparty|always")
                return
            pattern, mode = m.group(1), (m.group(2) or "").lower()
            item = self.trigger_item(pattern)
            if item is None:
                self.write_local(f"No trigger /{pattern}/.", "#cc6666")
                return
            if mode in ("party", "noparty"):
                item["mode"] = mode
            elif mode in ("always", ""):
                item.pop("mode", None)
                mode = "always"
            else:
                self.write_local(
                    "Mode must be party, noparty, or always.")
                return
            scope = self.cascade.source_of("triggers", pattern) \
                or "character"
            self.cascade.save_entry(scope, "triggers", None, item)
            self.load_layers()
            self.write_local(f"Trigger /{pattern}/ mode: {mode}")
        elif name == "var":
            if "=" in arg:
                k, v = (s.strip() for s in arg.split("=", 1))
                try:
                    v = int(v)
                except ValueError:
                    pass
                self.vars[k] = v
                self.write_local(f"var {k} = {v}")
            elif arg.strip():
                self.write_local(
                    f"var {arg.strip()} = "
                    f"{self.vars.get(arg.strip(), '(unset)')}")
            else:
                if not self.vars:
                    self.write_local("No vars set.")
                for k, v in sorted(self.vars.items()):
                    self.write_local(f"  {k} = {v}")
        elif name == "tellsound":
            a = arg.strip()
            if a:
                self.set_setting("tell_sound", a)
                self.load_layers()
                self.write_local(f"Tell sound: {a}")
                play_sound(a)
            else:
                self.write_local(
                    f"Tell sound: {self.setting('tell_sound', 'beep')}"
                    "   Set: #tellsound <path.wav|beep|off>")
        elif name == "trigsound":
            m = re.match(r"\s*/(.+?)/\s*(?:=\s*(.*))?$", arg)
            if not m:
                self.write_local(
                    "Usage: #trigsound /pattern/ = <path.wav|beep|off>")
                return
            pattern, snd = m.group(1), (m.group(2) or "").strip()
            item = self.trigger_item(pattern)
            if item is None:
                try:
                    re.compile(pattern)
                except re.error as e:
                    self.write_local(f"Bad regex: {e}", "#cc6666")
                    return
                item = {"pattern": pattern, "command": ""}
            item["sound"] = snd
            scope = self.cascade.source_of("triggers", pattern) \
                or "character"
            self.cascade.save_entry(scope, "triggers", None, item)
            self.load_layers()
            self.write_local(f"Trigger /{pattern}/ sound: "
                             f"{snd or '(none)'}")
            play_sound(snd)
        elif name == "histmin":
            a = arg.strip()
            if a.isdigit():
                self.set_setting("history_min_chars", int(a))
                self.load_layers()
                self.write_local(f"History keeps commands longer than "
                                 f"{a} chars.")
            else:
                self.write_local(
                    f"History min length: "
                    f"{self.setting('history_min_chars', 0)}   "
                    "Set: #histmin 3")
        elif name == "separator":
            a = arg.strip()
            if len(a) == 1 and not a.isalnum() and a != "#":
                self.set_setting("command_separator", a)
                self.load_layers()
                self.write_local(
                    f"Command separator is now '{a}' "
                    f"(doubled '{a}{a}' = literal).")
            elif a:
                self.write_local("Separator must be a single "
                                 "non-alphanumeric character (not #).")
            else:
                self.write_local(
                    f"Current separator: "
                    f"'{self.setting('command_separator', ';')}'  "
                    "Change: #separator /")
        elif name == "keys":
            order = sorted(self.keys)
            if not order:
                self.write_local("No keybindings.")
            for k in order:
                scope = self.cascade.source_of("keys", k) or "?"
                self.write_local(f"  {k:>12} -> {self.keys[k]}  "
                                 f"({scope})")
            self.write_local("Edit via Tools > Keybindings... or the "
                             "layer json files.")
        elif name == "guild":
            self.write_local(
                f"Guild: {self.guild}  file: "
                f"{paths.guild_file(self.mud, self.guild)}")
            self.write_local(
                "The profile's default guild is set in the picker (Edit "
                "profile). Swap live this session with Gswap <guild>.")
        elif name == "gswap":
            self.cmd_gswap(arg)
        elif name == "mip":
            self.mip_sent = False
            self.maybe_handshake("welcome")
        elif name == "deadman":
            a = arg.strip().lower()
            if a == "off" or a == "0":
                self.deadman_minutes = 0
                self.deadman_tripped = False
                self.write_local("Deadman disabled.")
            elif a.isdigit():
                self.deadman_minutes = int(a)
                self.write_local(f"Deadman set to {a} minutes.")
            else:
                self.write_local(
                    f"Deadman: {self.deadman_minutes}m. "
                    "#deadman <minutes|off>")
                return
            self.set_setting("deadman_minutes", self.deadman_minutes)
        elif name == "alias":
            if "=" in arg:
                aname, body = (s.strip() for s in arg.split("=", 1))
                if not aname:
                    self.write_local("Usage: #alias name = commands")
                    return
                self.quick_save_alias(aname, body)
                self.write_local(f"Alias '{aname}' = {body} "
                                 "(character scope)")
            elif arg.strip():
                if self.quick_delete_alias(arg.strip()):
                    self.write_local(f"Alias '{arg.strip()}' removed.")
                else:
                    self.write_local(f"No alias '{arg.strip()}'.")
            else:
                self.write_local("Usage: #alias name = commands")
        elif name == "aliases":
            if not self.aliases:
                self.write_local("No aliases defined.")
            for k, v in sorted(self.aliases.items()):
                scope = self.cascade.source_of("aliases", k) or "?"
                self.write_local(f"  ({scope}) {k} = {v}")
        elif name == "trigger":
            m = re.match(r"\s*/(.+?)/\s*(?:=\s*(.*))?$", arg)
            if not m:
                self.write_local("Usage: #trigger /regex/ = commands")
                return
            pattern, body = m.group(1), m.group(2)
            if body is None:
                if self.quick_delete_trigger(pattern):
                    self.write_local("Trigger removed.")
                else:
                    self.write_local("No such trigger.")
                return
            try:
                re.compile(pattern)
            except re.error as e:
                self.write_local(f"Bad regex: {e}", "#cc6666")
                return
            item = self.trigger_item(pattern) or {"pattern": pattern}
            item["command"] = body
            self.quick_save_trigger(item)
            self.write_local(f"Trigger /{pattern}/ = {body} "
                             "(character scope)")
        elif name == "triggers":
            if not self.triggers:
                self.write_local("No triggers defined.")
            for pattern, _c, body, snd, _cd in self.triggers:
                scope = self.cascade.source_of("triggers", pattern) \
                    or "?"
                extra = f"  [{snd}]" if snd else ""
                if self.trigger_modes.get(pattern):
                    extra += f"  [{self.trigger_modes[pattern]}]"
                self.write_local(
                    f"  ({scope}) /{pattern}/ = {body}{extra}")
        elif name == "log":
            if self.log_file:
                self.log_file.close(); self.log_file = None
                self.write_local("Logging stopped.")
            else:
                fname = arg.strip() or \
                    paths.session_log_file(self.character)
                self.log_file = open(fname, "a", encoding="utf-8")
                self.write_local(
                    "Logging EVERYTHING (text + MIP + LOCAL client lines + RATE "
                    f"replies) to {fname}")
        elif name == "go":
            if not arg.strip():
                self.write_local("Usage: #go <landmark>", "#cc6666")
            elif self.viking_go(arg.strip()):
                pass            # handled by guild-map coordinate nav
            else:
                self.start_go(arg.strip())
        elif name == "vmark":
            if arg.strip():
                self.add_viking_mark(arg.strip())
            else:
                self.write_local("Usage: #vmark <name>  (marks current "
                                 "guild-map cell)", "#cc6666")
        elif name == "vmarks":
            marks = viking.map_landmarks(self.viking_state,
                                         self.viking_marks())
            if not marks:
                self.write_local("No guild-map landmarks "
                                 "(POIs need mip_map on).")
            for nm, (x, y, label) in sorted(marks.items()):
                self.write_local(f"  ({label}) {nm} @ {x},{y}")
        elif name == "vn":
            self.cmd_vn(arg)
        elif name == "track":
            self.cmd_track(arg)
        elif name == "untrack":
            self.cmd_untrack(arg)
        elif name == "bot":
            self.cmd_bot(arg)
        elif name == "hunt":
            self.cmd_hunt(arg)
        elif name == "chaossea":
            self.cmd_chaossea(arg)
        elif name == "run":
            self.cmd_run(arg)
        elif name == "explore":
            self.cmd_explore(arg)
        elif name == "mapcheck":
            self.cmd_mapcheck(arg)
        elif name == "mapwipe":
            self.cmd_mapwipe(arg)
        elif name == "agent":
            self.cmd_agent(arg)
        elif name in self._macro_defs():
            self.cmd_macro(name, arg)
        elif name == "reagents":
            self.cmd_reagents(arg)
        elif name in ("mob", "mobs", "scan"):
            self.cmd_mob(arg)
        elif name == "stop":
            self._stop_all_automation("stopped")
        elif name == "speedruns":
            filt = arg.strip().lower()
            shown = 0
            for nm, entries in sorted(self.speedruns.items()):
                for rid, typ, desc in entries:
                    if filt and filt != typ.lower():
                        continue
                    tag = (f"{nm} x{len(entries)}"
                           if len(entries) > 1 else nm)
                    self.write_local(f"  {tag:<16} {typ:<9} {desc}")
                    shown += 1
            for nm, d in sorted(self.landmarks.items()):
                if filt and filt != "landmark":
                    continue
                self.write_local(f"  {nm:<16} {'landmark':<9} "
                                 f"{d.get('desc','')}")
                shown += 1
            self.write_local(f"{shown} entries. 'go <name>' to "
                             "travel; filters: shop mob area eq misc "
                             "clan crafting landmark")
        elif name == "landmark" and self.map_backend == "sqlite":
            self.sql_landmark(arg)
        elif name == "landmark":
            bits = arg.split(None, 2)
            if not bits:
                self.write_local("Usage: #landmark add <name> [desc] "
                                 "| #landmark del <name>", "#cc6666")
            elif bits[0] == "add" and len(bits) > 1:
                if not (self.locator and self.locator.located):
                    self.write_local("Not located on the map - can't "
                                     "save this room.", "#cc6666")
                else:
                    nm = bits[1].lower()
                    desc = bits[2] if len(bits) > 2 else self.room
                    self.landmarks[nm] = {"rid": self.locator.room_id,
                                          "desc": desc}
                    self.save_landmarks()
                    self.write_local(
                        f"Landmark '{nm}' saved (tt room "
                        f"{self.locator.room_id}). 'go {nm}' to "
                        "return.", "#66cc66")
            elif bits[0] == "del" and len(bits) > 1:
                if self.landmarks.pop(bits[1].lower(), None):
                    self.save_landmarks()
                    self.write_local(f"Landmark '{bits[1]}' deleted.")
                else:
                    self.write_local(f"No landmark '{bits[1]}'.",
                                     "#cc6666")
            else:
                self.write_local("Usage: #landmark add <name> [desc] "
                                 "| #landmark del <name>", "#cc6666")
        elif name in ("clearchat", "chatclear"):
            self.chat.configure(state="normal")
            self.chat.delete("1.0", "end")
            self.chat.configure(state="disabled")
            self.write_local("[chat/tell pane cleared]")
        elif name == "map" and self.map_backend == "sqlite":
            self.sql_map_command(arg.strip().lower())
        elif name == "map":
            a = arg.strip().lower()
            if a == "here":
                if self.locator and self.locator.located:
                    r = self.tmap.rooms[self.locator.room_id]
                    self.write_local(
                        f"Room {self.locator.room_id}: {r.name}  "
                        f"area: {r.area or '?'}"
                        + (f" [{r.coder}]" if r.coder else "")
                        + ("  (house)" if r.house else ""))
                    self.write_local(
                        "  patch syntax: {\"op\": \"redirect\", "
                        f"\"room\": {self.locator.room_id}, "
                        "\"exit\": \"<cmd>\", \"to\": <rid>}")
                else:
                    self.write_local("Not located.", "#cc6666")
            elif a in ("on", "start"):
                self.enter_mapping(auto=False)
            elif a == "new":
                mpath = paths.map_file(self.mud)
                if os.path.exists(mpath):
                    self.write_local(
                        f"[map] {os.path.basename(mpath)} already "
                        "exists - rename or delete it (and "
                        "landmarks.json / map-extra.json, whose room "
                        "ids belong to the old map) first.",
                        "#cc6666")
                elif not self.room:
                    self.write_local(
                        "[map] no current room data yet - walk into "
                        "a room so MIP reports it, then #map new.",
                        "#cc6666")
                else:
                    nm, exits, _v = mapdata.split_name(self.room)
                    use_exits = self.exits or exits
                    full = (f"{nm} ({','.join(use_exits)})"
                            if use_exits else nm)
                    with open(mpath, "w", encoding="utf-8") as f:
                        f.write("C 1000\n\n")
                        f.write("R {1} {0} {} {%s} {} {} {} {} {} {} "
                                "{1}\n" % full)
                    self.write_local(
                        f"[map] created {os.path.basename(mpath)} "
                        f"with seed room 1: {full}. Now '#map on' "
                        "and walk - new rooms persist as patches in "
                        "map-extra.json.", "#66cc66")
                    self.start_map_loader()
            elif a in ("off", "stop"):
                self.exit_mapping("manual")
            elif a == "rate":
                if self.locator and self.locator.located:
                    self.send_rating(self.locator.room_id)
                    self._pending_persist = None  # refresh only
                    self.write_local("[re-rating current room]",
                                     "#ffaa44")
                else:
                    self.write_local("Not located.", "#cc6666")
            else:
                self.write_local("Usage: #map here | on | off | rate")
        elif name == "record":
            self.arm_record(arg.strip().lower() or "character")
        elif name == "mapfix":
            if self.map_backend == "sqlite":
                self.sql_mapfix(arg)
            else:
                self.write_local("#mapfix is for the sqlite map backend "
                                 "only.", "#cc9933")
        elif name == "chart":
            if arg.strip():
                self.cmd_chartwalk(arg)      # Chart <entry> ... = auto-charter
            else:
                self.sql_chart_toggle()      # bare Chart = manual toggle
        elif name == "maproom":
            self.sql_maproom()
        elif name == "maprerate":
            self.sql_maprerate()
        elif name == "mapnote":
            self.sql_mapnote(arg)
        elif name == "maplink":
            self.sql_maplink(arg)
        elif name == "mapunlink":
            self.sql_mapunlink(arg)
        elif name in ("toggle", "toggles"):
            self.cmd_toggle("" if name == "toggles" else arg)
        elif name == "maploc" and self.map_backend == "sqlite":
            v = self._sql_cur_vnum
            self.write_local(
                f"Current room: {v if v is not None else 'unknown'}"
                + (" (uncharted)" if v is not None and self.mapdb
                   and not self.mapdb.has_room(v) else ""))
        elif name == "maploc":
            if not self.tmap:
                self.write_local("Map not loaded.")
            elif self.locator.located:
                r = self.tmap.rooms[self.locator.room_id]
                self.write_local(f"Located: tt room "
                                 f"{self.locator.room_id} - {r.name} "
                                 f"[{r.area}]")
            else:
                self.write_local(f"Not located. Candidates: "
                                 f"{self.locator.cands[:10]}")
        elif name == "help":
            self.show_help()
        else:
            self.write_local(f"Unknown client command #{name}. "
                             "Try #help.")

    def show_help(self):
        for line in HELP_TEXT.splitlines():
            self.write_local(line)

    # ------------------------------------------------ scripts
    def load_scripts(self, announce=True):
        self.hooks = None
        if not os.path.exists(paths.SCRIPTS_FILE):
            if announce:
                self.write_local("No katmud_scripts.py found.")
            return
        import importlib.util
        try:
            spec = importlib.util.spec_from_file_location(
                "katmud_scripts", paths.SCRIPTS_FILE)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.hooks = mod
            if announce:
                self.write_local("katmud_scripts.py loaded.",
                                 "#66cc66")
        except Exception as e:
            # ALWAYS show errors - a silently dead script file cost a
            # morning of debugging once (2026-06-10)
            self.write_local(f"Script error: {e}", "#cc6666")

    def call_hook(self, name, *args):
        if self.hooks and hasattr(self.hooks, name):
            try:
                return getattr(self.hooks, name)(self, *args)
            except Exception as e:
                self.write_local(f"Hook {name} error: {e}", "#cc6666")
        return None

    # Guild-scoped Agent hooks (agent_spec.md): agent_<name> on the active
    # guild's module overrides the engine default. Unlike call_hook (the user's
    # personal katmud_scripts surface) this is shipped guild code that travels
    # with the guild; it returns `default` on a missing/erroring hook so each
    # call site can name its fallback inline.
    GUILD_AGENT_MODULES = {"changelings": changeling}

    def agent_hook(self, name, *args, default=None):
        mod = self.GUILD_AGENT_MODULES.get(self.guild.lower())
        fn = getattr(mod, f"agent_{name}", None) if mod else None
        if fn is None:
            return default
        try:
            return fn(self, *args)
        except Exception as e:
            self.write_local(f"agent_{name} error: {e}", "#cc6666")
            return default

    # ------------------------------------------------ context menu
    def output_context_menu(self, event):
        idx = self.text.index(f"@{event.x},{event.y}")
        line = self.text.get(f"{idx} linestart", f"{idx} lineend")
        menu = tk.Menu(self.root, tearoff=0)
        if line.strip():
            menu.add_command(
                label="Trigger from this line...",
                command=lambda: dialogs.BuilderDialog(
                    self, prefill_pattern=re.escape(line.strip()),
                    sample_line=line.strip()))
            menu.add_command(
                label="Copy line",
                command=lambda: (self.root.clipboard_clear(),
                                 self.root.clipboard_append(line)))
        menu.add_command(label="Aliases && Triggers...",
                         command=lambda: dialogs.BuilderDialog(self))
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def restore_pane_sashes(self):
        """Put the right-column split (text|panes divider) and the chat/map/info
        splits back where they were left. Runs once the panes have a real size -
        sash_place is a no-op on a zero-size pane, so retry until laid out."""
        s = self.profiles_data.get("settings", {})
        sx = s.get("main_sash_x")
        rys = s.get("right_sash_y")
        if sx is None and not rys:
            return
        if self.paned.winfo_width() <= 1 or self.right_pane.winfo_height() <= 1:
            self.root.after(100, self.restore_pane_sashes)
            return
        try:
            if sx is not None:
                self.paned.sash_place(0, int(sx), 1)
            for i, y in enumerate(rys or []):
                self.right_pane.sash_place(i, 1, int(y))
        except (tk.TclError, ValueError, IndexError):
            pass

    def save_window_geometry(self):
        """Remember the main window's size/position/monitor (and maximized
        state) for next launch. When zoomed we keep the last NORMAL geometry
        so un-maximizing restores a sane size. Viking window saved too if
        open. Stored in the shared (global) settings, like fonts."""
        s = self.profiles_data.setdefault("settings", {})
        try:
            zoomed = self.root.state() == "zoomed"
            s["main_zoomed"] = zoomed
            if not zoomed:
                s["main_geometry"] = self.root.geometry()
            if self.viking_win is not None and \
                    self.viking_win.winfo_exists():
                s["viking_geometry"] = self.viking_win.geometry()
            # right-column sash positions: text|panes divider x, then the
            # chat/map/info divider ys (2 sashes for the 3 stacked panes).
            # Nested so a sash hiccup never blocks the geometry save.
            try:
                s["main_sash_x"] = self.paned.sash_coord(0)[0]
                s["right_sash_y"] = [self.right_pane.sash_coord(i)[1]
                                     for i in range(2)]
            except tk.TclError:
                pass
            profiles.save(self.profiles_data)
        except tk.TclError:
            pass

    def on_close(self):
        self.save_window_geometry()
        if self._maxes_dirty:
            self.persist_seen_max()
        if self.log_file:
            self.log_file.close()
        self.disconnect()
        self.root.destroy()


HELP_TEXT = """\
CLIENT COMMANDS
  #connect [host port] | #disconnect | #reload (cascade + scripts)
  #alias name = cmds | #alias name | #aliases   (character scope;
      use Tools > Aliases & Triggers for other scopes)
  #trigger /regex/ = cmds | #trigger /regex/ | #triggers
      (loop guards: a trigger that fires in a runaway loop is auto-suppressed;
      a flood of >25 trigger-fires in 3s PAUSES all triggers until #reload.
      A trigger's json may set "cooldown": <secs> to block fast refire, or
      "recursion_limit": <n> (0 = unlimited) to ALLOW a MUD-forced loop to fire
      that many times - it's also exempt from the flood breaker.)
  #trigmode /regex/ = party|noparty|always | #party [on|off]
  #gag /pattern/ | #gags | #keys
  #chatgag /pattern/ | #chatgags   (filter the chat/tell pane only)
  #deadman <minutes|off> | #mip | #mipraw | #markers
      (#markers: report room mob(italic)/player(underline) marker lines -
      needs 'aset look_monster italics' + 'aset look_player underline')
  #viking                         (Vikings: open the status window)
  #vmark <name> | #vmarks         (Vikings: guild-map coordinate
      landmarks; #go <name> walks there, or click the Map tab)
  VN <id> <start> <dest>          (Vikings: run a newbie fetch errand -
      accept <id>, walk to <start>, fetch, walk to <dest>, submit)
  Track <name> [low] | Untrack <name> | Track   (Necro: watch a power/
      reagent count in the info pane; dim-red when below <low>. Counts
      refresh from 'powers' / 'gs' readouts. Bladesinger: Track a skill
      (e.g. Track soul aegis) to see GXP still needed to raise it;
      costs refresh from the 'skills' readout, spendable GXP live.)
  #dmg [reset] | #var [name = value]
  #tellsound <path.wav|beep|off> | #trigsound /pattern/ = <snd>
  #histmin <n> | #separator <char>
  #go <name> | #stop | #speedruns [filter] | #maploc
  Bot | Bot mapless | Bot off   (roam the current room's AREA, fighting
      aggro mobs as they engage, then moving on; needs the sqlite map,
      autocombat, and the two room-marker asets - see #markers. Empty rooms
      are skipped instantly; rooms with a non-party player are ceded.
      'Bot mapless' drops the map requirement entirely - roams by direction
      only, for zones that can't be charted, e.g. every room sharing one
      vnum. No area-left stop; deadman trip stops in place, not walk-home.)
  Hunt | Hunt mapless | Hunt off   (like Bot but ACTIVELY attacks any mob
      it finds: uses guild setting autoattack_command if set (a '{t}' token
      is replaced with the mob keyword from the italic line), else
      'kill <keyword>'. Skips non-party players. Party = pwho roster +
      setting bot_party_whitelist. Same deadman return-to-start (mapless:
      stops in place instead). Biases movement toward the 3s minimap's mob
      cells when present (charted only - mapless has no minimap to bias
      from). Toggle flat makes it skip up/down exits. wander_clear_limit
      (default 30, 0=off) replaces hunt_clear_limit's no-combat stop when
      mapless.)
  Chaossea | Chaossea fight | Chaossea off  (Sea of Chaos: builds a
      session-local fake-room map from directions taken (no roomid - that
      zone shares one vnum and regenerates per visit). Movement always
      dives a 'down' exit on sight, else prefers unexplored directions,
      else BFS-retraces the temp map to the nearest still-unexplored room.
      Plain Chaossea: examines every mutant it meets ('examine mutant'/
      'mutant 2'/...), fights ones carrying 'a chaotic charm' or 'a cube of
      raw chaos' (whichever you still need), or that aggro/block you, loots
      via 'get all' + an explicit 'get charm' (bind-on-pickup), retreats
      ('retreat from the sea') and stops once you have both. Chaossea
      fight: kills every mutant it meets and ignores all drops, no stop
      condition besides deadman/manual off. Deadman trip stops either mode
      in place - no verified path home.)
  Run <bot> [loop] | Run | Run off   (fixed-route path bot from muds/<mud>/
      bots/*.json: walks the route; kills target mobs (whole-line match, or a
      keyword via "contains"); loots via after_kill; skips rooms with a
      non-party player or a skip_phrase; loops or returns to start. Bare Run
      lists bots / shows progress.)
  Explore | Explore off    (auto-chart the current area's open stubs: probes
      each unmapped exit through the charting gate, filling the map outward.
      Only cardinal/ordinal exits are auto-walked; up/down/enter/out etc. are
      flagged for manual review. Adjudicable mischarts (two exits to one room;
      two rooms reaching one room the same direction) are auto-re-charted.
      Unrated rooms are walked-to and rated, folding genuine holes into the
      area; overland borders rate area-less and are left. Short hops (<=2 rooms)
      stay gated/charting-on so a probe never charts from a stale room; only a
      jump >2 rooms away drops charting for a fast (arrival-verified) walk. When
      done it walks back to the room you started in. Needs the sqlite map; auto-
      enters Chart mode. Pauses in combat, stops on deadman - supervise it. Bare
      Explore = stubs left.)
  Mapcheck    (read-only audit of the current area: lists mischarts (reverse
      contradictions, same-direction collisions, dupe-target exits), unrated
      in-area rooms, and non-cardinal stubs without changing the map.)
  Mapwipe [confirm]   (DELETE all rooms+exits of the current area for a clean
      re-chart - the area name is kept, inbound doors become stubs. Bare
      Mapwipe previews; 'Mapwipe confirm' does it. Stop Explore first.)
  Agent [on|hunt|explore|off|debug]   (autonomous assistant: phased - charts
      the whole area (Explore) then sweeps it (Hunt), healing/fleeing via the
      active guild's profile, with a safety gate (area top class vs your best
      kill) before hunting. hunt/explore = that phase only. Deadman walks home
      then stops. Bare Agent = status; Agent debug = per-tick goal.)
  Gswap <guild> | Gswap    (switch the active guild live this session -
      reloads the cascade + guild hooks, no relaunch; bare Gswap lists guilds)
  Reagents [n]             (Necro: 'gs' then buy each reagent up to n
      (default 999), skipping bloodmoss, using the fresh gs counts)
  Vskills                  (Viking: send 'vskills' and READ it (not swallow)
      to refresh the Stats tab's GXP pools + per-skill training costs. Daler
      and the 4 pools stream live over MIP; the skill list updates on demand.)
  Missionlist               (Viking: send 'vmission list' and recommend
      which missions to accept for the highest total daler without
      exceeding current warehouse stock or your remaining daily quota.)
  VNlist [metric]            (Viking: send 'vmission newbie' and recommend
      the highest-'metric' newbie errands up to your remaining daily
      quota. metric is 'daler' (default) or one of timber/iron/furs/fish/
      grain/mead/amber - newbie errands cost nothing to accept, so it's a
      plain top-N pick, no stock to conflict over.)
  Scan [name] | #mobs | #mob <name>   (mob database: Scan/#mob <name>
      searches, no arg = count. Any character can look up; auto-filled from
      Din's 'vscan1' reports, keyed by name+area of the current room.)
  #landmark add <name> [desc] | #landmark del <name>
  #map here | on | off | rate | new   (mapping mode & room info;
      'new' bootstraps a blank map from the current room)
  #clearchat                      (clear the chat/tell pane)
  speedwalk: line starts with ';' -> ;15el8esr(open gate)q
      letters from cascading 'speedwalk' map (see global.json);
      anything unmapped goes in (); counts apply to either
  #record [scope]                 (record next observed edge; tin maps)
  #mapfix <dir> cmd|setup|return|wait <value> | <dir> clear
      (sqlite maps: special exits)
  Chart                  (sqlite: toggle map-building mode - location is
      proven by the room packet and input is gated one move at a time;
      Esc clears the queue. Following mode otherwise tracks you live.)
  Chart <entry> [return] [simple] | Chart off   (one-shot auto-charter: take
      <entry> into an area, chart every room + complete every stub (bounded to
      the entry room's rated area), then return to start and report. 'simple' =
      compass exits only; optional <return> cmd for an asymmetric entry. Pauses
      in combat, stops on deadman/#stop - supervise it.)
  Maproom | Mapnote <text> | Mapnote clear     (room details / notes)
  Maplink <dir> [command] <destvnum> | Mapunlink <dir>|<command>
      (manually fix an exit; bare Go/Landmark/Mapfix/Chart/Map* work
      without the #; lowercase passes through to the MUD)
  Toggle [name] | Toggles          (list/flip side-pane switches:
      charting = map building, mapdetail = room info in the pane,
      necroguard = auto con/protection/veil (Necro); switches persist
      per character, except charting which boots off)
  #log [file] (capture EVERYTHING: text + MIP; toggle) | #help
SCRIPT HOOKS (katmud_scripts.py, #reload to refresh)
  on_connect(client) | on_line(client, clean) |
  on_command(client, cmd) | on_mip(client, tag, data) |
  on_vitals(client, v) | on_tick(client)
"""


def run(profile_id):
    paths.ensure_dirs()
    data, err = profiles.load()
    entry = profiles.get(data, profile_id)
    if entry is None:
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showerror("KatMUD",
                     f"No profile '{profile_id}' in profiles.json."
                     + (f"\n(profiles.json error: {err})" if err
                        else ""))
        root.destroy()
        return
    root = tk.Tk()
    app = MudClient(root, entry, data)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
