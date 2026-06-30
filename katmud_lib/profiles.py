"""katmud_lib.profiles - profiles.json, the character picker data.

Spec section 2.1. Entry shape:
    {
      "id": "3s-Normal",
      "display_name": "Normal (3s Viking)",
      "character": "Normal",
      "mud": "3s",
      "guild": "vikings",
      "config_override": null,
      "last_launched": "2026-06-12T09:30:00"
    }

No passwords in this file, ever. Always safe to zip and share.
"""

import datetime
import os

from . import config, paths

DEFAULT = {
    "_comment": [
        "katmud character picker data. NEVER contains passwords -",
        "those live in the OS credential store (katmud/<mud>/<char>).",
        "Safe to zip, share, and sync.",
    ],
    "profiles": [],
    "settings": {},
}


def load():
    data, err = paths.load_json(paths.PROFILES_FILE, default=DEFAULT)
    data.setdefault("profiles", [])
    data.setdefault("settings", {})
    return data, err


def save(data):
    paths.save_json(paths.PROFILES_FILE, data)


def get(data, profile_id):
    for p in data["profiles"]:
        if p.get("id") == profile_id:
            return p
    return None


def make_id(character, mud):
    return f"{paths.safe_name(mud)}-{character.strip()}"


def ordered_for_picker(data):
    """Spec 1.3: up to 3 most-recently-used at top, then the full list
    alphabetical grouped by mud (which implies port). Returns
    (mru_list, [(mud, [profiles...]), ...]) - MRU entries repeat in
    the grouped list so the full list stays complete."""
    profiles = list(data["profiles"])
    with_ts = [p for p in profiles if p.get("last_launched")]
    with_ts.sort(key=lambda p: p["last_launched"], reverse=True)
    mru = with_ts[:3]
    groups = {}
    for p in profiles:
        groups.setdefault(p.get("mud", "?"), []).append(p)
    grouped = []
    for mud in sorted(groups):
        grouped.append((mud, sorted(
            groups[mud],
            key=lambda p: (p.get("display_name") or p.get("id", "")
                           ).lower())))
    return mru, grouped


def touch(data, profile_id):
    p = get(data, profile_id)
    if p:
        p["last_launched"] = datetime.datetime.now() \
            .isoformat(timespec="seconds")
        save(data)


def delete(data, profile_id):
    """Remove a profile entry. Does NOT touch the character file or
    stored credential - those may be shared by sibling profiles."""
    data["profiles"] = [p for p in data["profiles"]
                        if p.get("id") != profile_id]
    save(data)


def upsert(data, entry):
    """Insert or replace by id, scaffold the character file if absent
    (spec 1.3 / 2.3: an existing characters/<name>.json is REUSED, not
    scaffolded over - that's what makes gswap profiles share one
    personal layer)."""
    existing = get(data, entry["id"])
    if existing:
        existing.update(entry)
    else:
        data["profiles"].append(entry)
    cpath = paths.character_file(entry["character"])
    if not os.path.exists(cpath):
        paths.save_json(cpath, config.CHARACTER_TEMPLATE)
    save(data)
