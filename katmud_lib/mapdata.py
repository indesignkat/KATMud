"""katmud_lib.mapdata - tt++ map support for KatMUD.

Ported from pymud_map v6, extended for the v7 spec:

  * Graph patches (spec 6.2): cascade layers declare exit overrides /
    additions / new rooms; applied to the in-memory graph AT LOAD, so
    pathfinder, locator, and renderer all see a normal graph.
  * House scoping (6.2.1): [House] rooms and every edge touching them
    are character-scoped patches, never written to shared map data.
  * Mapping mode (6.3): new rooms created from MIP room data + rating
    capture, persisted as patches - mud scope in
    muds/<mud>/map-extra.json, character scope in the character file.
  * Areas (6.4/6.5): rooms carry area name + coder; renderer filters
    to the current area (done client-side via Room.area/coder).

Patch record shapes (in any layer's "map_patches" list, or in
map-extra.json's "patches" list):

  {"op": "add_room", "room": 900001, "name": "A dusty ledge (n,d)",
   "area": "Cliffs of Vrek", "coder": "thoreau", "house": false}
  {"op": "add_edge", "room": 900001, "exit": "n", "to": 48112}
  {"op": "redirect", "room": 48112, "exit": "west", "to": 19204}

Patch room ids live at PATCH_RID_BASE and up so they can never
collide with tt++ session ids.

3k vnums are opaque node ids (spec open item) - nothing here assumes
they mean anything beyond uniqueness within the file.
"""

import collections
import os
import re

from . import paths

PATCH_RID_BASE = 900000


def split_braces(line):
    """Split a tt++ record line into top-level {...} fields.
    Handles nested braces: '{a} {{mobs} {Cancer}} {b}' -> 3 fields."""
    fields = []
    depth = 0
    cur = []
    for ch in line:
        if ch == "{":
            depth += 1
            if depth == 1:
                cur = []
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                fields.append("".join(cur))
                continue
        if depth >= 1:
            cur.append(ch)
    return fields


GRID_DIRS = {
    "n": (0, -1), "s": (0, 1), "e": (1, 0), "w": (-1, 0),
    "ne": (1, -1), "nw": (-1, -1), "se": (1, 1), "sw": (-1, 1),
}

ROOM_AVOID = 1
ROOM_VOID = 8
ROOM_BLOCK = 1 << 15
EXIT_AVOID = 2
EXIT_BLOCK = 8

DIR_TO_BITS = {"n": 1, "e": 2, "s": 4, "w": 8, "u": 16, "d": 32,
               "ne": 3, "se": 6, "sw": 12, "nw": 9}


def dir_offset(dirbits):
    return ((1 if dirbits & 2 else 0) - (1 if dirbits & 8 else 0),
            (1 if dirbits & 4 else 0) - (1 if dirbits & 1 else 0))


class Room:
    __slots__ = ("rid", "name", "exits", "emeta", "area", "coder",
                 "note", "flags", "patched", "house")

    def __init__(self, rid, name, area="", note="", flags=0,
                 coder="", patched=False, house=False):
        self.rid = rid
        self.name = name          # full "Room Name (e,w,n)" as captured
        self.exits = {}           # command -> target room id
        self.emeta = {}           # command -> (dirbits, exit flags)
        self.area = area
        self.coder = coder
        self.note = note
        self.flags = flags
        self.patched = patched    # came from a patch layer
        self.house = house        # character-scoped house room


def norm_key(name, exits):
    return (name.strip().lower(), tuple(sorted(e.strip().lower()
                                               for e in exits if e.strip())))


NAME_EXITS_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
VNUM_SUFFIX_RE = re.compile(r"^(.*?)~(\d+)\s*$")
AREA_CODER_RE = re.compile(r"^(.*?)\s*\[([^\]]*)\]\s*$")


def split_name(full):
    """'The Center of Town (d,n,s,w,e)' -> ('The Center of Town',
    ['d','n','s','w','e'], None). Some captures kept the MIP suffix:
    'A vast training field (e,w,n,s)~51' -> (..., [...], 51)."""
    full = full.strip()
    vnum = None
    vm = VNUM_SUFFIX_RE.match(full)
    if vm:
        full, vnum = vm.group(1).strip(), int(vm.group(2))
    m = NAME_EXITS_RE.match(full)
    if not m:
        return full, [], vnum
    return m.group(1), [e.strip() for e in m.group(2).split(",")
                        if e.strip()], vnum


def split_area(area_field):
    """tt area field or rating capture: 'Cliffs of Vrek [thoreau]'
    -> ('Cliffs of Vrek', 'thoreau')."""
    m = AREA_CODER_RE.match(area_field.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return area_field.strip(), ""


class TinMap:
    def __init__(self, path, patches=None):
        """patches: ordered list of (scope, patch_dict). Applied after
        parsing, in order - later patches win, mirroring the cascade."""
        self.rooms = {}
        self.by_key = collections.defaultdict(list)
        self.by_name = collections.defaultdict(list)
        self.by_vnum = collections.defaultdict(list)
        self.next_patch_rid = PATCH_RID_BASE
        self.patch_errors = []
        self._parse(path)
        for scope, patch in (patches or []):
            self.apply_patch(patch, scope)

    # ----------------------------------------------------- indexing
    def _index(self, room):
        if not room.name:
            return
        nm, exits, vnum = split_name(room.name)
        room.name = f"{nm} ({','.join(exits)})" if exits else nm
        self.by_key[norm_key(nm, exits)].append(room.rid)
        self.by_name[nm.lower()].append(room.rid)
        if vnum is not None:
            self.by_vnum[vnum].append(room.rid)

    def _unindex(self, room):
        nm, exits, _v = split_name(room.name)
        for idx, key in ((self.by_key, norm_key(nm, exits)),
                         (self.by_name, nm.lower())):
            if room.rid in idx.get(key, []):
                idx[key].remove(room.rid)

    def _parse(self, path):
        cur = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("R "):
                    fl = split_braces(line)
                    # R {id} {flags} {color} {name} {symbol} {desc}
                    #   {area} {note} {terrain} {data} {weight} {?}
                    if len(fl) < 4:
                        cur = None
                        continue
                    try:
                        rid = int(fl[0])
                    except ValueError:
                        cur = None
                        continue
                    name = fl[3].strip()
                    try:
                        rflags = int(fl[1].strip() or 0)
                    except ValueError:
                        rflags = 0
                    area_field = fl[6].strip() if len(fl) > 6 else ""
                    area, coder = split_area(area_field)
                    note = fl[7].strip() if len(fl) > 7 else ""
                    cur = Room(rid, name, area, note, rflags,
                               coder=coder)
                    self.rooms[rid] = cur
                    self._index(cur)
                    if rid >= self.next_patch_rid:
                        self.next_patch_rid = rid + 1
                elif line.startswith("E ") and cur is not None:
                    fl = split_braces(line)
                    # E {target} {name} {cmd} {dirbits} ...
                    if len(fl) < 3:
                        continue
                    try:
                        tgt = int(fl[0])
                    except ValueError:
                        continue
                    cmd = fl[2].strip() or fl[1].strip()
                    if cmd:
                        cur.exits[cmd] = tgt
                        try:
                            dirbits = int(fl[3].strip() or 0) \
                                if len(fl) > 3 else 0
                        except ValueError:
                            dirbits = 0
                        try:
                            eflags = int(fl[4].strip() or 0) \
                                if len(fl) > 4 else 0
                        except ValueError:
                            eflags = 0
                        cur.emeta[cmd] = (dirbits, eflags)
                else:
                    if not line.startswith(" "):
                        cur = None if not line.startswith("E") else cur

    # ------------------------------------------------------ patches
    def apply_patch(self, patch, scope="?"):
        """Apply one graph patch dict. Errors are collected, not
        raised - a bad hand-edit shouldn't kill the map load."""
        op = patch.get("op", "redirect")
        try:
            if op == "add_room":
                rid = int(patch["room"])
                name = patch.get("name", f"patched room {rid}")
                room = Room(rid, name,
                            area=patch.get("area", ""),
                            coder=patch.get("coder", ""),
                            patched=True,
                            house=bool(patch.get("house")))
                if rid in self.rooms:
                    self._unindex(self.rooms[rid])
                self.rooms[rid] = room
                self._index(room)
                if rid >= self.next_patch_rid:
                    self.next_patch_rid = rid + 1
            elif op in ("add_edge", "redirect"):
                rid = int(patch["room"])
                cmd = str(patch["exit"]).strip()
                tgt = int(patch["to"])
                room = self.rooms.get(rid)
                if room is None:
                    self.patch_errors.append(
                        f"[{scope}] {op}: room {rid} not on map")
                    return
                dirbits = DIR_TO_BITS.get(cmd.lower(), 0)
                if cmd in room.emeta:
                    dirbits = room.emeta[cmd][0] or dirbits
                room.exits[cmd] = tgt
                room.emeta[cmd] = (dirbits, 0)
            elif op == "del_edge":
                rid = int(patch["room"])
                cmd = str(patch["exit"]).strip()
                room = self.rooms.get(rid)
                if room:
                    room.exits.pop(cmd, None)
                    room.emeta.pop(cmd, None)
            else:
                self.patch_errors.append(f"[{scope}] unknown op {op!r}")
        except (KeyError, ValueError, TypeError) as e:
            self.patch_errors.append(f"[{scope}] bad patch {patch}: {e}")

    def alloc_rid(self):
        rid = self.next_patch_rid
        self.next_patch_rid += 1
        return rid

    # ------------------------------------------------------- lookups
    def candidates(self, name, exits):
        ids = self.by_key.get(norm_key(name, exits))
        if ids:
            return list(ids)
        return list(self.by_name.get(name.strip().lower(), []))

    # ---------------------------------------------------- traversal
    def tunnel(self, from_rid, tgt, dirbits=0):
        ox = oy = 0
        seen = set()
        while True:
            room = self.rooms.get(tgt)
            if room is None or not room.flags & ROOM_VOID \
                    or tgt in seen:
                return tgt, (ox, oy)
            seen.add(tgt)
            items = list(room.exits.items())
            chosen = None
            if len(items) == 2:
                for cmd, t2 in items:
                    if t2 != from_rid:
                        chosen = (cmd, t2)
                        break
            else:
                for cmd, t2 in items:
                    if room.emeta.get(cmd, (0, 0))[0] == dirbits \
                            and dirbits:
                        chosen = (cmd, t2)
                        break
            if chosen is None:
                return tgt, (ox, oy)
            cmd, nxt = chosen
            d2 = room.emeta.get(cmd, (0, 0))[0]
            dx, dy = dir_offset(d2)
            ox += dx
            oy += dy
            from_rid, tgt, dirbits = tgt, nxt, d2

    def follow(self, rid, cmd):
        room = self.rooms.get(rid)
        if room is None:
            return None
        tgt = room.exits.get(cmd)
        if tgt is None:
            return None
        dirbits = room.emeta.get(cmd, (0, 0))[0]
        return self.tunnel(rid, tgt, dirbits)[0]

    def iter_exits(self, rid, respect_avoid=True):
        room = self.rooms.get(rid)
        if room is None:
            return
        for cmd, tgt in room.exits.items():
            dirbits, eflags = room.emeta.get(cmd, (0, 0))
            if respect_avoid and eflags & (EXIT_AVOID | EXIT_BLOCK):
                continue
            final, off = self.tunnel(rid, tgt, dirbits)
            fro = self.rooms.get(final)
            if fro is None:
                continue
            if respect_avoid and fro.flags & (ROOM_AVOID | ROOM_BLOCK):
                continue
            ex, ey = dir_offset(dirbits)
            yield cmd, final, (ex + off[0], ey + off[1])

    # ------------------------------------------------------- pathing
    def path(self, src, dst, max_rooms=200000, respect_avoid=True):
        if src == dst:
            return []
        prev = {src: None}
        q = collections.deque([src])
        seen = 1
        while q and seen < max_rooms:
            rid = q.popleft()
            for cmd, tgt, _off in self.iter_exits(rid, respect_avoid):
                if tgt in prev:
                    continue
                prev[tgt] = (rid, cmd)
                if tgt == dst:
                    out = []
                    node = dst
                    while prev[node] is not None:
                        node, c = prev[node]
                        out.append(c)
                    out.reverse()
                    return out
                q.append(tgt)
                seen += 1
        return None

    def path_any(self, src, dst):
        p = self.path(src, dst)
        if p is not None:
            return p, False
        p = self.path(src, dst, respect_avoid=False)
        return p, p is not None

    def path_nearest(self, src, dsts, max_rooms=200000,
                     respect_avoid=True):
        dset = set(dsts)
        if src in dset:
            return [], src
        prev = {src: None}
        q = collections.deque([src])
        seen = 1
        while q and seen < max_rooms:
            rid = q.popleft()
            for cmd, tgt, _off in self.iter_exits(rid, respect_avoid):
                if tgt in prev:
                    continue
                prev[tgt] = (rid, cmd)
                if tgt in dset:
                    out = []
                    node = tgt
                    while prev[node] is not None:
                        node, c = prev[node]
                        out.append(c)
                    out.reverse()
                    return out, tgt
                q.append(tgt)
                seen += 1
        return None, None

    def path_any_nearest(self, src, dsts):
        p, d = self.path_nearest(src, dsts)
        if p is not None:
            return p, d, False
        p, d = self.path_nearest(src, dsts, respect_avoid=False)
        return p, d, p is not None

    # ------------------------------------------------------- map view
    def neighborhood(self, center, radius=3, same_area_only=True):
        """Lay rooms onto a grid around `center`. Renderer draws only
        the current area (spec 6.4) - spatially interleaved areas
        never co-render. Rooms with no recorded area are treated as
        area-compatible (most of the base map predates area capture).
        On residual collision: last-drawn-wins handled by the renderer
        via the 'collide' flag here - punt visibly, not confusingly."""
        c_room = self.rooms.get(center)
        c_area = c_room.area if c_room else ""
        grid = {(0, 0): center}
        placed = {center: (0, 0)}
        collisions = set()
        q = collections.deque([center])
        while q:
            rid = q.popleft()
            x, y = placed[rid]
            for cmd, tgt, (dx, dy) in self.iter_exits(rid):
                if (dx, dy) == (0, 0):
                    off = GRID_DIRS.get(cmd.lower())
                    if off is None:
                        continue
                    dx, dy = off
                if tgt in placed:
                    continue
                t_room = self.rooms.get(tgt)
                if same_area_only and c_area and t_room and \
                        t_room.area and t_room.area != c_area:
                    continue
                nx, ny = x + dx, y + dy
                if abs(nx) > radius or abs(ny) > radius:
                    continue
                if (nx, ny) in grid:
                    collisions.add((nx, ny))
                    grid[(nx, ny)] = tgt        # last-drawn-wins
                    placed[tgt] = (nx, ny)
                    continue
                grid[(nx, ny)] = tgt
                placed[tgt] = (nx, ny)
                q.append(tgt)
        return grid, collisions


class Locator:
    """Tracks which tt++ room the player is in. Unchanged from v6,
    plus a confidence flag for auto-mapping entry (spec 6.3: auto-entry
    requires a confident fix on the PREVIOUS known room)."""

    def __init__(self, tmap):
        self.tmap = tmap
        self.room_id = None
        self.cands = []
        self.prev_confident = False   # had a single-room fix last step

    @property
    def located(self):
        return self.room_id is not None

    def on_room(self, full_or_name, exits, last_cmd=None, vnum=None):
        self.prev_confident = self.located
        name, parsed, embedded = split_name(full_or_name)
        if not exits:
            exits = parsed
        if vnum is None:
            vnum = embedded

        if vnum is not None:
            for rid in self.tmap.by_vnum.get(vnum, ()):
                room = self.tmap.rooms.get(rid)
                if room and split_name(room.name)[0].lower() \
                        == name.strip().lower():
                    self.room_id = rid
                    self.cands = []
                    return rid

        matches = self.tmap.candidates(name, exits)

        if self.room_id is not None and last_cmd:
            tgt = self.tmap.follow(self.room_id, last_cmd.strip())
            if tgt is not None and tgt in matches:
                self.room_id = tgt
                self.cands = []
                return self.room_id

        if len(matches) == 1:
            self.room_id = matches[0]
            self.cands = []
            return self.room_id

        if not matches:
            self.room_id = None
            self.cands = []
            return None

        if last_cmd and self.cands:
            reachable = set()
            for cid in self.cands:
                tgt = self.tmap.follow(cid, last_cmd.strip())
                if tgt is not None:
                    reachable.add(tgt)
            narrowed = [m for m in matches if m in reachable]
            if len(narrowed) == 1:
                self.room_id = narrowed[0]
                self.cands = []
                return self.room_id
            if narrowed:
                matches = narrowed

        self.room_id = None
        self.cands = matches
        return None

    def force(self, rid):
        """Pin the locator to a known room (mapping mode just created
        it from observed data, so this IS a confident fix)."""
        self.room_id = rid
        self.cands = []


# ==========================================================================
# Patch persistence
# ==========================================================================
def extra_file(mud):
    return os.path.join(paths.mud_dir(mud), "map-extra.json")


def load_extra_patches(mud):
    """Mud-scope patch sidecar (mapping-mode output). Returns
    (patches_list, error_or_None)."""
    data, err = paths.load_json(extra_file(mud),
                                default={"patches": []})
    return data.get("patches", []), err


def append_extra_patches(mud, new_patches):
    def mutate(data):
        data.setdefault("patches", []).extend(new_patches)
    _d, err = paths.update_json(extra_file(mud), mutate,
                                default={"patches": []})
    return err


def collect_patches(cascade, mud):
    """All patches for a map load, cascade order: mud sidecar first,
    then each cascade layer's map_patches (global->mud->guild->char),
    so character patches (houses) win. Returns list of (scope, patch)."""
    out = []
    extra, err = load_extra_patches(mud)
    if err:
        out_err = [err]
    else:
        out_err = []
    for p in extra:
        out.append(("mud-extra", p))
    for scope in cascade.active_scopes():
        layer = cascade.layers.get(scope) or {}
        for p in layer.get("map_patches", []):
            out.append((scope, p))
    return out, out_err


SPEEDRUN_RE = re.compile(
    r"\.add_speedrun\s+\{([^}]*)\}\s+\{([^}]*)\}\s+\{(\d+)\}\s+\{([^}]*)\}")


def load_speedruns(path):
    """Parse .add_speedrun {name} {type} {tt_vnum} {desc} lines.
    Returns {name: [(tt_vnum, type, desc), ...]} - duplicate names
    are kept (one per physical destination); 'go' walks to nearest."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = SPEEDRUN_RE.search(line)
            if m:
                name, typ, vnum, desc = m.groups()
                name = name.strip().lower()
                if name:
                    entry = (int(vnum), typ.strip(), desc.strip())
                    out.setdefault(name, [])
                    if entry not in out[name]:
                        out[name].append(entry)
    return out
