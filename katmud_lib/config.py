"""katmud_lib.config - the four-layer configuration cascade (spec s2).

Load order (later wins):
  1. global.json
  2. muds/<mud>/mud.json
  3. muds/<mud>/guilds/<guild>.json     (skipped when guild == "none")
  4. characters/<character>.json

Collision rule for ALL config types, MIP handlers included:
REPLACE, not append. A later layer's entry with the same key fully
supplants the earlier one.

Keyed-ness per section:
  aliases   - dict, key = alias name
  macros    - dict, key = macro name -> ordered list of gated steps
              ({"cmd": "...", "wait": "<substring or null>"}), e.g. a
              bladesinger's multi-rune "autoregen {x}". Run with the
              capitalized command name (Autoregen <item>); {x} in a
              step's cmd is replaced with the typed argument.
  triggers  - list of dicts, key = "pattern"
  gags      - list of strings, key = the string itself
  chat_gags - list of strings (chat/tell pane filter), keyed likewise
  keys      - dict, keysym -> command
  mip       - dict, tag -> handler declaration
  vitals    - list (whole-list replace: a layer that defines vitals
              replaces the entire bar row, because bar layout is a
              unit, not a mergeable set)
  login_commands - whole-list replace, same reasoning
  setup_commands - ordered-set merge (additive across layers, deduped):
                   mud-wide login-time setup that every guild needs,
                   e.g. 3s's room-marker asets + DISPLAY_ROOMID
  map_patches    - list of dicts, key = (room, exit) - section 6.2
  connection / settings - dict, per-key replace

settings of note:
  map_backend - "tin" (default; tt++ .map via mapdata.py) or "sqlite"
                (per-mud SQLite map via mapsql.py, used for 3s). Set in
                muds/<mud>/mud.json. Selects which mapping engine a mud
                uses; see mapping_redesign_spec.md.

Every layer file is a flat dict of those sections; mud.json adds
"connection" ({host, port}). Unknown keys are preserved untouched by
the read-modify-write path in paths.py and simply carried through the
merge so future sections need no migration.
"""

from . import paths

LAYER_SCOPES = ("global", "mud", "guild", "character")

# Sections where the merge is dict.update (per-key replace).
DICT_SECTIONS = ("aliases", "macros", "keys", "mip", "settings", "connection",
                 "speedwalk",
                 # corpse: the on-kill loot routine. keys solo/party (command
                 # strings) + optional toggle (gate on a Toggle switch, e.g.
                 # "harvest"). Per-key merge so a character can override one
                 # guild's command without rewriting the rest.
                 "corpse",
                 "vars")
# Sections that are keyed lists: later same-key entry replaces earlier.
KEYED_LIST_SECTIONS = {
    "triggers": lambda item: item.get("pattern"),
    "map_patches": lambda item: (item.get("room"), item.get("exit")),
}
# Plain string lists merged as ordered sets (later layer can also
# remove an inherited entry by listing it under "<section>_remove").
# setup_commands: mud/guild/character-wide commands sent ONCE on login
# (additive across layers, unlike login_commands which whole-replaces) -
# e.g. 3s's `aset look_monster italics` / `aset look_player underline` /
# `setmod DISPLAY_ROOMID 1`, which must fire for every guild.
SET_LIST_SECTIONS = ("gags", "chat_gags", "kill_patterns",
                     "setup_commands")
# Whole-value replace: if the layer defines it at all, it wins.
REPLACE_SECTIONS = ("vitals", "login_commands")

CHARACTER_TEMPLATE = {
    "_comment": [
        "Personal layer for this character. Loaded last - wins all",
        "collisions. DISCIPLINE RULE (spec 2.4): guild-specific config",
        "belongs in the guild file, never here, or guild-switching",
        "breaks its promise.",
    ],
    "aliases": {},
    "triggers": [],
    "gags": [],
    "chat_gags": [],
    "keys": {},
    "mip": {},
    "settings": {},
    "map_patches": [],
}


def layer_path(scope, mud=None, guild=None, character=None):
    if scope == "global":
        return paths.GLOBAL_FILE
    if scope == "mud":
        return paths.mud_config_file(mud)
    if scope == "guild":
        return paths.guild_file(mud, guild)
    if scope == "character":
        return paths.character_file(character)
    raise ValueError(f"unknown scope {scope!r}")


def scope_label(scope, mud=None, guild=None, character=None):
    """Concrete labels for the builder UI: '3s / Warders / Kethric'."""
    return {"global": "global (all muds)",
            "mud": f"{mud} (mud-wide)",
            "guild": f"{mud} / {guild}",
            "character": f"{character} (character)"}[scope]


class Cascade:
    """Loads the four layers and produces the merged view.

    merged  - dict of sections after the collision rules above
    sources - {(section, key): scope} so the UI can show where each
              entry came from and the builder can edit in place
    errors  - list of human-readable load problems (missing guild file
              is a WARNING here, not fatal - spec 2.2)
    """

    def __init__(self, mud, guild, character):
        self.mud = mud
        self.guild = (guild or "none")
        self.character = character
        self.layers = {}        # scope -> raw layer dict
        self.errors = []
        self.warnings = []
        self.merged = {}
        self.sources = {}
        self.reload()

    # ---------------------------------------------------------- load
    def active_scopes(self):
        scopes = ["global", "mud"]
        if self.guild.lower() != "none":
            scopes.append("guild")
        scopes.append("character")
        return scopes

    def path_for(self, scope):
        return layer_path(scope, mud=self.mud, guild=self.guild,
                          character=self.character)

    def label_for(self, scope):
        return scope_label(scope, mud=self.mud, guild=self.guild,
                           character=self.character)

    def reload(self):
        self.layers.clear()
        self.errors.clear()
        self.warnings.clear()
        for scope in self.active_scopes():
            path = self.path_for(scope)
            data, err = paths.load_json(path, default={})
            if err:
                self.errors.append(err)
            elif scope == "guild" and not data:
                # spec 2.2: missing guild file = visible warning,
                # not fatal.
                import os
                if not os.path.exists(path):
                    self.warnings.append(
                        f"guild file missing: {path} - guild layer "
                        "skipped (create it or set guild to none)")
            self.layers[scope] = data
        self._merge()

    # --------------------------------------------------------- merge
    def _merge(self):
        merged = {}
        sources = {}
        for scope in self.active_scopes():
            layer = self.layers.get(scope) or {}
            for section, value in layer.items():
                if section.startswith("_"):
                    continue
                if section in DICT_SECTIONS:
                    dst = merged.setdefault(section, {})
                    if isinstance(value, dict):
                        for k, v in value.items():
                            dst[k] = v
                            sources[(section, k)] = scope
                elif section in KEYED_LIST_SECTIONS:
                    keyfn = KEYED_LIST_SECTIONS[section]
                    dst = merged.setdefault(section, [])
                    if isinstance(value, list):
                        for item in value:
                            if not isinstance(item, dict):
                                continue
                            k = keyfn(item)
                            dst[:] = [d for d in dst if keyfn(d) != k]
                            dst.append(item)
                            sources[(section, k)] = scope
                elif section in SET_LIST_SECTIONS:
                    dst = merged.setdefault(section, [])
                    if isinstance(value, list):
                        for item in value:
                            if item not in dst:
                                dst.append(item)
                                sources[(section, item)] = scope
                elif section in REPLACE_SECTIONS:
                    merged[section] = value
                    sources[(section, None)] = scope
                else:
                    # Unknown section: carry through, whole-replace.
                    merged[section] = value
                    sources[(section, None)] = scope
            # removals: "<section>_remove": [keys...] in any layer
            for section in list(layer.keys()):
                if not section.endswith("_remove"):
                    continue
                base = section[:-7]
                keys = layer[section]
                if not isinstance(keys, list):
                    continue
                if base in DICT_SECTIONS:
                    for k in keys:
                        merged.get(base, {}).pop(k, None)
                elif base in KEYED_LIST_SECTIONS:
                    keyfn = KEYED_LIST_SECTIONS[base]
                    merged[base] = [d for d in merged.get(base, [])
                                    if keyfn(d) not in keys]
                elif base in SET_LIST_SECTIONS:
                    merged[base] = [s for s in merged.get(base, [])
                                    if s not in keys]
        self.merged = merged
        self.sources = sources

    # ----------------------------------------------- builder support
    def get(self, section, default=None):
        return self.merged.get(section, default)

    def source_of(self, section, key=None):
        return self.sources.get((section, key))

    def save_entry(self, scope, section, key, value):
        """Write one entry into the chosen layer file via
        read-modify-write, then reload the cascade.
        For dict sections: key = dict key, value = entry.
        For keyed lists: key ignored, value = the item dict.
        For set lists: value = the string.
        Returns error string or None."""
        path = self.path_for(scope)

        def mutate(data):
            if section in DICT_SECTIONS:
                data.setdefault(section, {})[key] = value
            elif section in KEYED_LIST_SECTIONS:
                keyfn = KEYED_LIST_SECTIONS[section]
                lst = data.setdefault(section, [])
                k = keyfn(value)
                lst[:] = [d for d in lst if keyfn(d) != k]
                lst.append(value)
            elif section in SET_LIST_SECTIONS:
                lst = data.setdefault(section, [])
                if value not in lst:
                    lst.append(value)
            else:
                data[section] = value

        default = (CHARACTER_TEMPLATE if scope == "character" else {})
        _data, err = paths.update_json(path, mutate, default=default)
        if not err:
            self.reload()
        return err

    def delete_entry(self, scope, section, key):
        """Remove one entry from the chosen layer file. Returns error
        string or None ('not found' is not an error)."""
        path = self.path_for(scope)

        def mutate(data):
            if section in DICT_SECTIONS:
                data.get(section, {}).pop(key, None)
            elif section in KEYED_LIST_SECTIONS:
                keyfn = KEYED_LIST_SECTIONS[section]
                if section in data:
                    data[section] = [d for d in data[section]
                                     if keyfn(d) != key]
            elif section in SET_LIST_SECTIONS:
                if section in data and key in data[section]:
                    data[section].remove(key)

        _data, err = paths.update_json(path, mutate)
        if not err:
            self.reload()
        return err

    def move_entry(self, from_scope, to_scope, section, key):
        """One-click promotion (spec section 4): copy the entry to the
        new scope, then delete it from the old. Returns error or None."""
        layer = self.layers.get(from_scope) or {}
        value = None
        if section in DICT_SECTIONS:
            value = layer.get(section, {}).get(key)
        elif section in KEYED_LIST_SECTIONS:
            keyfn = KEYED_LIST_SECTIONS[section]
            for item in layer.get(section, []):
                if keyfn(item) == key:
                    value = item
                    break
        elif section in SET_LIST_SECTIONS:
            if key in layer.get(section, []):
                value = key
        if value is None:
            return (f"{section} entry {key!r} not found in "
                    f"{from_scope} layer")
        err = self.save_entry(to_scope, section, key, value)
        if err:
            return err
        return self.delete_entry(from_scope, section, key)
