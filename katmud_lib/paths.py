"""katmud_lib.paths - the v7 directory layout (spec section 7).

Everything that touches the filesystem goes through here so the layout
is defined exactly once.

    katmud/
      katmud.pyw
      profiles.json            # picker data - NEVER contains passwords
      global.json              # universal cascade layer
      muds/<mud>/mud.json      # connection + mud-wide layer
      muds/<mud>/<mud>.map
      muds/<mud>/speedruns-<mud>.tin
      muds/<mud>/guilds/<guild>.json
      characters/<character>.json
      logs/crash.log
      logs/<character>-<date>.log
"""

import json
import os
import re

# katmud_lib/ sits directly under the install root.
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROFILES_FILE = os.path.join(BASE, "profiles.json")
GLOBAL_FILE = os.path.join(BASE, "global.json")
# Cross-character reminders: a shared file every client process reads, so a
# `Reminder` set on one character fires in whatever client is open when it's
# due (survives logins, character switches, and relaunches).
REMINDERS_FILE = os.path.join(BASE, "reminders.json")
# Phone/web dashboard (docs/superpowers/specs/2026-06-21-web-dashboard-design.md):
# each character writes <profile_id>.json here every tick; the hub writes
# <profile_id>.cmd when a command is submitted from the phone. Two separate
# files, not one shared field, because each is written by exactly one
# process - no read-modify-write race is possible on either.
WEBSTATE_DIR = os.path.join(BASE, "webstate")
MUDS_DIR = os.path.join(BASE, "muds")
CHARACTERS_DIR = os.path.join(BASE, "characters")
LOGS_DIR = os.path.join(BASE, "logs")
SCRIPTS_FILE = os.path.join(BASE, "katmud_scripts.py")
CRASH_LOG = os.path.join(LOGS_DIR, "crash.log")

# Credential Manager namespace (spec rename note overrides the older
# "pymud/" wording in section 3).
KEYRING_SERVICE = "katmud"


def safe_name(name):
    """Filesystem-safe lowercase token for guild/character filenames."""
    return re.sub(r"[^a-z0-9_-]", "", name.lower().replace(" ", "_"))


def mud_dir(mud):
    return os.path.join(MUDS_DIR, safe_name(mud))


def mud_config_file(mud):
    return os.path.join(mud_dir(mud), "mud.json")


def guild_dir(mud):
    return os.path.join(mud_dir(mud), "guilds")


def guild_file(mud, guild):
    return os.path.join(guild_dir(mud), f"{safe_name(guild)}.json")


def character_file(character):
    return os.path.join(CHARACTERS_DIR, f"{safe_name(character)}.json")


def map_file(mud):
    return os.path.join(mud_dir(mud), f"{safe_name(mud)}.map")


def speedruns_file(mud):
    return os.path.join(mud_dir(mud), f"speedruns-{safe_name(mud)}.tin")


def map_db_file(mud):
    """SQLite map database for a mud (3s mapping redesign). Separate
    file per mud - vnum spaces are never shared across muds."""
    return os.path.join(mud_dir(mud), f"{safe_name(mud)}.db")


def webstate_file(profile_id):
    return os.path.join(WEBSTATE_DIR, f"{safe_name(profile_id)}.json")


def webcmd_file(profile_id):
    return os.path.join(WEBSTATE_DIR, f"{safe_name(profile_id)}.cmd")


def write_webcmd(profile_id, text):
    """Hub-side: the only writer of <profile_id>.cmd."""
    os.makedirs(WEBSTATE_DIR, exist_ok=True)
    save_json(webcmd_file(profile_id), text)


def claim_webcmd(profile_id):
    """Character-side: read and delete the pending command, or None if
    there isn't one. Deleting on claim is what makes this race-free
    against the hub (which only ever creates/overwrites the file) -
    there's nothing left for a second claim to read."""
    path = webcmd_file(profile_id)
    if not os.path.exists(path):
        return None
    data, err = load_json(path)
    try:
        os.remove(path)
    except OSError:
        pass
    return None if err else data


def session_log_file(character):
    import time
    return os.path.join(
        LOGS_DIR, f"{safe_name(character)}-{time.strftime('%Y%m%d')}.log")


def list_muds():
    """Mud names = subdirectories of muds/ that contain a mud.json."""
    out = []
    if os.path.isdir(MUDS_DIR):
        for entry in sorted(os.listdir(MUDS_DIR)):
            if os.path.isfile(mud_config_file(entry)):
                out.append(entry)
    return out


def list_guilds(mud):
    """Guild names available for a mud = guilds/*.json (sans extension),
    skipping files that start with underscore (templates/examples)."""
    gdir = guild_dir(mud)
    out = []
    if os.path.isdir(gdir):
        for fn in sorted(os.listdir(gdir)):
            if fn.endswith(".json") and not fn.startswith("_"):
                out.append(fn[:-5])
    return out


def ensure_dirs():
    for d in (MUDS_DIR, CHARACTERS_DIR, LOGS_DIR, WEBSTATE_DIR):
        os.makedirs(d, exist_ok=True)


# --------------------------------------------------------------------
# Read-modify-write JSON helpers (spec section 4: "preserve unknown
# keys, stable ordering" so hand-edits and machine-edits coexist).
# json.load keeps insertion order; we only ever update keys in place
# or append, never rebuild the dict - hand-authored ordering and any
# keys the client doesn't understand survive every save.
# --------------------------------------------------------------------
def load_json(path, default=None):
    """Load a json file. Returns (data, error_string_or_None).
    Missing file -> (copy of default, None). Parse error -> the error,
    so callers can show it instead of silently nuking a hand-edit."""
    if not os.path.exists(path):
        return (json.loads(json.dumps(default)) if default is not None
                else {}), None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except (OSError, json.JSONDecodeError) as e:
        return (json.loads(json.dumps(default)) if default is not None
                else {}), f"{path}: {e}"


def save_json(path, data):
    """Atomic-ish save: write temp then replace, so a crash mid-write
    never leaves a truncated config behind."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def update_json(path, mutate, default=None):
    """Read-modify-write: load, call mutate(data) in place, save.
    Returns (data, error). On parse error the file is NOT overwritten
    - we refuse to destroy a hand-edit that merely has a typo."""
    data, err = load_json(path, default if default is not None else {})
    if err:
        return data, err
    mutate(data)
    save_json(path, data)
    return data, None
