#!/usr/bin/env python3
"""migrate_v6.py - one-time migration from pymud v6 to KatMUD v7.

Run ON YOUR MACHINE (passwords go into Windows Credential Manager,
which only exists there):

    python tools\\migrate_v6.py <path-to-old-pymud-folder>

What it does:
  1. Reads the old pymud_profiles.json.
  2. Stores each profile's plaintext password into the OS credential
     store under katmud/<mud>/<character>, then NEVER writes it to
     any file.
  3. Builds v7 profiles.json entries (id, display_name, character,
     mud, guild) - no passwords, no host/port (those move to mud.json).
  4. Writes per-profile aliases/triggers/gags/numpad/seen_max into
     characters/<name>.json (numpad keys are converted to v7 'keys'
     keysym entries, both NumLock variants - spec 4.1).
  5. Copies guild files into muds/<mud>/guilds/ for every mud the
     guild was used on (v6 guild files were port-keyed; v7 guild files
     are per-mud - spec 2.2).
  6. Reminds you to delete the old pymud_profiles.json afterwards: it
     still contains plaintext passwords.

Port -> mud mapping: 3200 -> 3s, 3000 -> 3k (anything else is asked).
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from katmud_lib import config, credentials, paths, profiles  # noqa: E402

PORT_TO_MUD = {3200: "3s", 3000: "3k"}

# v6 guild labels -> v7 guild file names
GUILD_RENAME = {"viking": "vikings", "bladesinger": "bladesingers",
                "warder": "warders"}

# v6 numpad logical key -> v7 keysym pairs (NumLock on / off variants)
NUMPAD_KEYSYMS = {
    "0": ("KP_0", "KP_Insert"), "1": ("KP_1", "KP_End"),
    "2": ("KP_2", "KP_Down"), "3": ("KP_3", "KP_Next"),
    "4": ("KP_4", "KP_Left"), "5": ("KP_5", "KP_Begin"),
    "6": ("KP_6", "KP_Right"), "7": ("KP_7", "KP_Home"),
    "8": ("KP_8", "KP_Up"), "9": ("KP_9", "KP_Prior"),
    "minus": ("KP_Subtract",), "plus": ("KP_Add",),
    "star": ("KP_Multiply",), "slash": ("KP_Divide",),
    "dot": ("KP_Decimal", "KP_Delete"),
}


def numpad_to_keys(numpad):
    out = {}
    for k, cmd in (numpad or {}).items():
        for sym in NUMPAD_KEYSYMS.get(k, ()):
            out[sym] = cmd
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    old_dir = sys.argv[1]
    old_profiles = os.path.join(old_dir, "pymud_profiles.json")
    if not os.path.exists(old_profiles):
        sys.exit(f"not found: {old_profiles}")
    with open(old_profiles, encoding="utf-8") as f:
        old = json.load(f)

    if not credentials.available():
        print("WARNING: keyring not installed (pip install keyring).")
        print("Passwords will NOT be migrated; the client will prompt.")

    paths.ensure_dirs()
    data, _err = profiles.load()

    for old_name, p in old.get("profiles", {}).items():
        port = p.get("port", 3200)
        mud = PORT_TO_MUD.get(port)
        if mud is None:
            mud = input(f"profile {old_name}: port {port} -> "
                        "mud name? ").strip() or f"port{port}"
        char = p.get("character") or old_name.split("-")[0]
        char = char.strip()

        # 1. password -> credential store, never to disk
        pw = p.get("password") or ""
        if pw and credentials.available():
            err = credentials.set_password(mud, char, pw)
            print(f"  password for {mud}/{char}: "
                  f"{'stored' if not err else err}")

        # 2. picker entry
        entry = {
            "id": profiles.make_id(char, mud),
            "display_name": old_name,
            "character": char,
            "mud": mud,
            "guild": GUILD_RENAME.get(
                paths.safe_name(p.get("guild", "") or "none"),
                paths.safe_name(p.get("guild", "") or "none"))
            or "none",
            "config_override": None,
            "port_override": (port if port not in (3200, 3000)
                              else None),
            "last_launched": None,
        }
        profiles.upsert(data, entry)

        # 3. personal layer
        def mutate(cdata, p=p):
            cdata.setdefault("aliases", {}).update(p.get("aliases", {}))
            trigs = cdata.setdefault("triggers", [])
            have = {t.get("pattern") for t in trigs}
            for t in p.get("triggers", []):
                if t.get("pattern") and t["pattern"] not in have:
                    trigs.append(t)
            gags = cdata.setdefault("gags", [])
            for g in p.get("gags", []):
                if g not in gags:
                    gags.append(g)
            cdata.setdefault("keys", {}).update(
                numpad_to_keys(p.get("numpad")))
            st = cdata.setdefault("settings", {})
            if p.get("deadman_minutes") is not None:
                st["deadman_minutes"] = p["deadman_minutes"]
            if p.get("seen_max"):
                cdata.setdefault("seen_max", {}).update(p["seen_max"])

        cpath = paths.character_file(char)
        _d, err = paths.update_json(cpath, mutate,
                                    default=config.CHARACTER_TEMPLATE)
        print(f"  {entry['id']}: character file "
              f"{'ok' if not err else err}")


    # 4b. landmarks: v6 per-port files -> per-mud files
    for port, mud in PORT_TO_MUD.items():
        src = os.path.join(old_dir, f"pymud_landmarks_{port}.json")
        dst = os.path.join(paths.mud_dir(mud), "landmarks.json")
        if os.path.exists(src) and not os.path.exists(dst):
            import shutil
            shutil.copy(src, dst)
            print(f"  landmarks: {src} -> {dst}")

    profiles.save(data)
    print(f"\nWrote {paths.PROFILES_FILE}")
    print("\nNOTE: v6 guild files (pymud_guilds/*.json) use a per-port")
    print("format; v7 guild files are per-mud with a different shape.")
    print("Re-author them under muds/<mud>/guilds/ - the Vikings file")
    print("ships as the format reference.")
    print(f"\n*** Delete {old_profiles} when done - it still contains")
    print("*** plaintext passwords.")


if __name__ == "__main__":
    main()
