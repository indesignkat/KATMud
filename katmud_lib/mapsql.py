"""katmud_lib.mapsql - SQLite mapping/pathfinding data layer (3s).

Stage 1 of mapping_redesign_spec.md: the schema and a connection /
loader module ONLY. No charting, parsing, or pathfinding yet - those
arrive in later stages. This module is deliberately standalone (stdlib
+ paths only) so it can be reviewed and tested in isolation.

Why this exists: 3Scapes embeds real room vnums in short descriptions
and exposes a `rating` command, so its map can be built automatically
through normal play rather than from a hand-maintained tt++ `.map`
file (see mapdata.py, which still serves 3k). 3s starts with an EMPTY
database and fills it in-game; 3k is converted in later, into its own
separate database with its own vnum space - the two are never
reconciled.

Backend selection lives in config as the per-mud `map_backend` setting
("tin" default, "sqlite"); nothing here reads it. A mud's database file
is paths.map_db_file(mud).

Schema (spec "Schema (SQLite)"):
  areas     (area_id PK, name, author)
  rooms     (vnum PK, short_desc, area_id FK, x, y, z)
  exits     (from_vnum FK, direction, to_vnum FK, command,
             setup_command, return_command, wait_seconds)
            PRIMARY KEY (from_vnum, direction)
  landmarks (tag, vnum FK, scope, scope_owner)
            Landmarks are MUD-WIDE: one namespace per map, shared by every
            character/guild on that mud. The scope/scope_owner columns are
            legacy (kept so old databases open) and are always 'mud'/NULL on
            new writes; the v4 migration collapses any old per-player/guild
            rows to mud scope.

`exits.direction` is a canonical token (n/e/s/w/u/d/ne/nw/se/sw/
p/l/r/o, or a static non-compass label like "left"); `command` /
`setup_command` / `return_command` carry the literal text to send when
it differs from the token (spec "mapfix"). wait_seconds > 0 marks an
exit pathfinding must never route through (spec pathfinding rules).
"""

import heapq
import os
import sqlite3

# Bump when the schema changes; later stages migrate on this.
# v2: rooms.notes (free-text player notes, set via the `Mapnote` command).
# v3: mobs table (vscan1 capture); identity = (name, area_name).
# v4: landmarks are mud-wide - collapse any old player/guild rows to mud scope.
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS areas (
    area_id INTEGER PRIMARY KEY,
    name    TEXT,
    author  TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
    vnum       INTEGER PRIMARY KEY,
    short_desc TEXT,
    area_id    INTEGER REFERENCES areas(area_id),
    x          INTEGER,
    y          INTEGER,
    z          INTEGER,
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS exits (
    from_vnum      INTEGER NOT NULL REFERENCES rooms(vnum),
    direction      TEXT    NOT NULL,
    to_vnum        INTEGER REFERENCES rooms(vnum),
    command        TEXT,
    setup_command  TEXT,
    return_command TEXT,
    wait_seconds   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (from_vnum, direction)
);

CREATE TABLE IF NOT EXISTS landmarks (
    tag         TEXT    NOT NULL,
    vnum        INTEGER REFERENCES rooms(vnum),
    scope       TEXT    NOT NULL,
    scope_owner TEXT,
    UNIQUE (tag, scope, scope_owner)
);

CREATE TABLE IF NOT EXISTS mobs (
    mob_id         INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    area_id        INTEGER REFERENCES areas(area_id),
    area_name      TEXT NOT NULL DEFAULT '',
    race           TEXT,
    alignment      INTEGER,
    class          INTEGER,
    off_edged      INTEGER, off_blunt    INTEGER, off_fire     INTEGER,
    off_ice        INTEGER, off_acid     INTEGER, off_electric INTEGER,
    off_mind       INTEGER, off_energy   INTEGER, off_poison   INTEGER,
    off_radiation  INTEGER,
    attack_pattern TEXT,
    penetration    INTEGER,
    custom_attacks INTEGER,
    special_chance INTEGER,
    special_damage TEXT,
    special_wc     TEXT,
    def_edged      INTEGER, def_blunt    INTEGER, def_fire     INTEGER,
    def_ice        INTEGER, def_acid     INTEGER, def_electric INTEGER,
    def_mind       INTEGER, def_energy   INTEGER, def_poison   INTEGER,
    def_radiation  INTEGER,
    lives          TEXT,
    dodge          INTEGER,
    defense        INTEGER,
    regeneration   INTEGER,
    hunts          INTEGER, aggressive   INTEGER, switches     INTEGER,
    moves          INTEGER, peaceable    INTEGER,
    first_seen     TEXT,
    last_seen      TEXT,
    scanned_by     TEXT,
    UNIQUE (name, area_name)
);
"""

# Every column a parsed vscan row may set (everything but the identity/
# bookkeeping columns the writer manages itself).
MOB_DATA_COLUMNS = (
    "race", "alignment", "class",
    "off_edged", "off_blunt", "off_fire", "off_ice", "off_acid",
    "off_electric", "off_mind", "off_energy", "off_poison", "off_radiation",
    "attack_pattern", "penetration", "custom_attacks",
    "special_chance", "special_damage", "special_wc",
    "def_edged", "def_blunt", "def_fire", "def_ice", "def_acid",
    "def_electric", "def_mind", "def_energy", "def_poison", "def_radiation",
    "lives", "dodge", "defense", "regeneration",
    "hunts", "aggressive", "switches", "moves", "peaceable",
)

# Columns mapfix may edit on an exit (whitelist for set_exit_field).
EXIT_EDIT_FIELDS = ("command", "setup_command", "return_command",
                    "wait_seconds")

# Geometric reverse of a compass direction, for labelling the synthetic
# reverse edge built from an exit's return_command (label only - the
# return command is what's actually sent).
REVERSE_DIRS = {"n": "s", "s": "n", "e": "w", "w": "e",
                "ne": "sw", "sw": "ne", "nw": "se", "se": "nw",
                "u": "d", "d": "u"}

# Planar grid offsets for laying a neighborhood onto the map pane
# (mirrors mapdata.GRID_DIRS). u/d and static labels have no planar
# offset and are skipped for placement (their rooms are reached, just
# not positioned by direction).
GRID_DIRS = {"n": (0, -1), "s": (0, 1), "e": (1, 0), "w": (-1, 0),
             "ne": (1, -1), "nw": (-1, -1), "se": (1, 1), "sw": (-1, 1)}


class MapDB:
    """Owns one SQLite connection for a single mud's map.

    Opening is create-if-missing: the schema is applied with
    CREATE TABLE IF NOT EXISTS, so a brand-new (empty) database - the
    normal 3s starting state - and an existing one take the same path.
    """

    def __init__(self, path):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # WAL for crash-resilience (not concurrency: a mud is never
        # connected twice at once). FKs off by default in sqlite3.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _user_version(self):
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self):
        """Bring an existing database up to SCHEMA_VERSION. A brand-new db
        (user_version 0) already matches SCHEMA (CREATE ... above), so it
        only needs its version stamped. Older databases get the
        incremental ALTERs they're missing. CREATE TABLE IF NOT EXISTS
        never adds a column to an existing table, so new columns must be
        ALTERed in here."""
        ver = self._user_version()
        if ver and ver < 2:
            cols = {r["name"] for r in
                    self.conn.execute("PRAGMA table_info(rooms)")}
            if "notes" not in cols:
                self.conn.execute("ALTER TABLE rooms ADD COLUMN notes TEXT")
        # v3 adds the `mobs` table only - a new table, so the
        # CREATE TABLE IF NOT EXISTS in SCHEMA above already created it on
        # this open; nothing to ALTER, just stamp the version below.
        if ver and ver < 4:
            # Landmarks are now mud-wide. Collapse any per-player/guild rows
            # to mud scope. Tag is the mud-wide key, so de-dup first (keep the
            # earliest-created row per tag) to avoid a UNIQUE collision, then
            # promote the survivors.
            self.conn.execute(
                "DELETE FROM landmarks WHERE rowid NOT IN "
                "(SELECT MIN(rowid) FROM landmarks GROUP BY tag)")
            self.conn.execute(
                "UPDATE landmarks SET scope='mud', scope_owner=NULL")
        if ver != SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # ------------------------------------------------------ lifecycle
    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    # ------------------------------------------------------- writers
    # Thin upserts - no business logic; charting/parsing decides the
    # values in later stages. "Upsert" = insert or replace the row's
    # mutable columns, keyed by the primary key.
    def upsert_area(self, area_id, name=None, author=None):
        self.conn.execute(
            "INSERT INTO areas (area_id, name, author) VALUES (?,?,?) "
            "ON CONFLICT(area_id) DO UPDATE SET name=excluded.name, "
            "author=excluded.author",
            (area_id, name, author))
        self.conn.commit()

    def upsert_room(self, vnum, short_desc=None, area_id=None,
                    x=None, y=None, z=None):
        self.conn.execute(
            "INSERT INTO rooms (vnum, short_desc, area_id, x, y, z) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(vnum) DO UPDATE SET "
            "short_desc=excluded.short_desc, area_id=excluded.area_id, "
            "x=excluded.x, y=excluded.y, z=excluded.z",
            (vnum, short_desc, area_id, x, y, z))
        self.conn.commit()

    def migrate_vnum(self, old, new):
        """Re-key a room from `old` to `new`, carrying its exits (both
        directions) and landmarks. Used to repair rooms charted under a
        truncated 5-digit id once the full 6-digit id is recovered from a
        BAD bracket (3s truncates the tilde id; see mapparse.is_truncated_id).
        No-op if `new` already exists (caller must merge/skip that case) or
        ids are equal. FKs are toggled off for the re-key so updating the
        parent vnum doesn't trip the child references mid-statement."""
        if old == new or self.has_room(new) or not self.has_room(old):
            return
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("UPDATE rooms SET vnum=? WHERE vnum=?",
                              (new, old))
            self.conn.execute("UPDATE exits SET from_vnum=? WHERE from_vnum=?",
                              (new, old))
            self.conn.execute("UPDATE exits SET to_vnum=? WHERE to_vnum=?",
                              (new, old))
            self.conn.execute("UPDATE landmarks SET vnum=? WHERE vnum=?",
                              (new, old))
            self.conn.commit()
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def set_room_note(self, vnum, notes):
        """Set (or clear, with notes=None) the free-text note on a room.
        Kept separate from upsert_room so charting a room never touches
        the player's notes, and vice-versa."""
        self.conn.execute("UPDATE rooms SET notes=? WHERE vnum=?",
                          (notes, vnum))
        self.conn.commit()

    def upsert_exit(self, from_vnum, direction, to_vnum=None,
                    command=None, setup_command=None,
                    return_command=None, wait_seconds=0):
        self.conn.execute(
            "INSERT INTO exits (from_vnum, direction, to_vnum, command, "
            "setup_command, return_command, wait_seconds) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(from_vnum, direction) DO UPDATE SET "
            "to_vnum=excluded.to_vnum, command=excluded.command, "
            "setup_command=excluded.setup_command, "
            "return_command=excluded.return_command, "
            "wait_seconds=excluded.wait_seconds",
            (from_vnum, direction, to_vnum, command, setup_command,
             return_command, wait_seconds))
        self.conn.commit()

    # Landmarks are mud-wide: one namespace per map, keyed by tag. The
    # scope/scope_owner columns are legacy and always written 'mud'/NULL.
    # We delete-then-insert rather than upsert: the table's UNIQUE is on
    # (tag, scope, scope_owner), and SQLite treats the NULL scope_owner as
    # distinct, so ON CONFLICT would never fire and a re-add would duplicate
    # the tag. Keying on tag alone keeps one row per landmark.
    def add_landmark(self, tag, vnum):
        self.conn.execute("DELETE FROM landmarks WHERE tag=?", (tag,))
        self.conn.execute(
            "INSERT INTO landmarks (tag, vnum, scope, scope_owner) "
            "VALUES (?,?,'mud',NULL)", (tag, vnum))
        self.conn.commit()

    def remove_landmark(self, tag):
        self.conn.execute("DELETE FROM landmarks WHERE tag=?", (tag,))
        self.conn.commit()

    def landmarks_for(self):
        """All landmarks on this mud (one mud-wide namespace)."""
        return list(self.conn.execute(
            "SELECT tag, vnum, scope, scope_owner FROM landmarks"))

    def exit_to(self, from_vnum, to_vnum):
        """An exit row from `from_vnum` whose far side is `to_vnum`, or
        None. Used for single-step `go` (stage 5) before full pathing."""
        return self.conn.execute(
            "SELECT * FROM exits WHERE from_vnum=? AND to_vnum=?",
            (from_vnum, to_vnum)).fetchone()

    def get_exit(self, from_vnum, direction):
        return self.conn.execute(
            "SELECT * FROM exits WHERE from_vnum=? AND direction=?",
            (from_vnum, direction)).fetchone()

    def set_exit_field(self, from_vnum, direction, field, value):
        """mapfix: set ONE special-exit column (creating the stub if
        needed) without disturbing the others. `field` is whitelisted."""
        if field not in EXIT_EDIT_FIELDS:
            raise ValueError(f"bad exit field {field!r}")
        self.ensure_exit(from_vnum, direction)
        self.conn.execute(
            f"UPDATE exits SET {field}=? "
            "WHERE from_vnum=? AND direction=?",
            (value, from_vnum, direction))
        self.conn.commit()

    # --------------------------------------------------- pathfinding (6)
    def _usable_edges(self, u):
        """(neighbour_vnum, edge) pairs usable from room u for routing:
        charted non-timed exits, plus the synthetic reverse of any special
        exit whose return_command can bring us back from its far side."""
        cands = []
        for row in self.conn.execute(
                "SELECT * FROM exits WHERE from_vnum=? "
                "AND to_vnum IS NOT NULL AND wait_seconds=0", (u,)):
            cands.append((row["to_vnum"], row))
        for row in self.conn.execute(
                "SELECT * FROM exits WHERE to_vnum=? "
                "AND return_command IS NOT NULL AND wait_seconds=0", (u,)):
            cands.append((row["from_vnum"], {
                "direction": REVERSE_DIRS.get(row["direction"],
                                              row["direction"]),
                "command": row["return_command"],
                "setup_command": None}))
        return cands

    def find_path(self, src, dst):
        """Dijkstra over charted, non-timed exits (to_vnum set,
        wait_seconds == 0). Unit step cost - among usable exits, fewest
        steps wins. Returns the ordered list of exit rows to traverse,
        [] if already there, or None if `dst` is unreachable WITHOUT a
        timed/excluded exit (spec: no fallback through those)."""
        if src == dst:
            return []
        dist = {src: 0}
        prev = {}
        pq = [(0, src)]
        seen = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == dst:
                break
            for v, edge in self._usable_edges(u):
                nd = d + 1
                if nd < dist.get(v, 1 << 30):
                    dist[v] = nd
                    prev[v] = (u, edge)
                    heapq.heappush(pq, (nd, v))
        if dst not in prev:
            return None
        return self.reconstruct_path(prev, src, dst)

    def paths_from(self, src):
        """Single-source shortest-path TREE from `src` over usable edges, in
        ONE pass (Dijkstra, unit cost). Returns (dist, prev): dist[v] = hop
        count, prev[v] = (u, edge). Reconstruct any route with
        reconstruct_path(prev, src, dst). O(rooms+edges) - far cheaper than
        calling find_path once per candidate destination (the Explore
        nearest-stub scan was O(stubs) Dijkstras = O(stubs^2) edges, which
        froze the UI on big areas)."""
        dist = {src: 0}
        prev = {}
        pq = [(0, src)]
        seen = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            for v, edge in self._usable_edges(u):
                nd = d + 1
                if nd < dist.get(v, 1 << 30):
                    dist[v] = nd
                    prev[v] = (u, edge)
                    heapq.heappush(pq, (nd, v))
        return dist, prev

    @staticmethod
    def reconstruct_path(prev, src, dst):
        """Edge list from src to dst given a `prev` tree (see paths_from):
        [] if src==dst, or None if dst isn't reachable in the tree."""
        if src == dst:
            return []
        if dst not in prev:
            return None
        edges = []
        node = dst
        while node != src:
            u, row = prev[node]
            edges.append(row)
            node = u
        edges.reverse()
        return edges


    # --------------------------------------------------- charting (4)
    # Higher-level helpers the auto-charting layer uses. Kept distinct
    # from upsert_exit: charting must NEVER clobber a special exit's
    # command/setup/return (mapfix data) when it merely learns the far
    # side of an exit, so these touch only the columns they own.
    def get_or_create_area(self, name, author=None):
        """Area id for `name`, creating the row (autoincrement id) if
        new. Backfills a missing author when one is later observed."""
        row = self.conn.execute(
            "SELECT area_id, author FROM areas WHERE name=?",
            (name,)).fetchone()
        if row is not None:
            if author and not row["author"]:
                self.conn.execute(
                    "UPDATE areas SET author=? WHERE area_id=?",
                    (author, row["area_id"]))
                self.conn.commit()
            return row["area_id"]
        cur = self.conn.execute(
            "INSERT INTO areas (name, author) VALUES (?,?)",
            (name, author))
        self.conn.commit()
        return cur.lastrowid

    def set_room_area(self, vnum, area_id):
        self.conn.execute("UPDATE rooms SET area_id=? WHERE vnum=?",
                          (area_id, vnum))
        self.conn.commit()

    # ----------------------------------------------------- mobs (v3)
    def upsert_mob(self, mob, area_id, area_name, scanned_by, now):
        """Insert a vscan'd mob, or refresh an existing one. Identity is
        (name, area_name) - the user's rule: area names are unique, so area
        implies coder, and class/stats vary so they aren't part of identity.
        `mob` is a dict of MOB_DATA_COLUMNS (missing keys -> NULL). Returns
        True if a new row was inserted, False if an existing one updated."""
        cols = [c for c in MOB_DATA_COLUMNS if c in mob]
        vals = [mob[c] for c in cols]
        row = self.conn.execute(
            "SELECT mob_id FROM mobs WHERE name=? AND area_name=?",
            (mob.get("name", ""), area_name or "")).fetchone()
        if row is None:
            allcols = ["name", "area_id", "area_name"] + cols + \
                ["first_seen", "last_seen", "scanned_by"]
            allvals = [mob.get("name", ""), area_id, area_name or ""] + \
                vals + [now, now, scanned_by]
            ph = ",".join("?" * len(allcols))
            self.conn.execute(
                f"INSERT INTO mobs ({','.join(allcols)}) VALUES ({ph})",
                allvals)
            self.conn.commit()
            return True
        sets = ["area_id=?"] + [f"{c}=?" for c in cols] + ["last_seen=?",
                                                           "scanned_by=?"]
        params = [area_id] + vals + [now, scanned_by, row["mob_id"]]
        self.conn.execute(
            f"UPDATE mobs SET {','.join(sets)} WHERE mob_id=?", params)
        self.conn.commit()
        return False

    def get_mob(self, name, area_name):
        return self.conn.execute(
            "SELECT * FROM mobs WHERE name=? AND area_name=?",
            (name, area_name or "")).fetchone()

    def mob_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM mobs").fetchone()[0]

    def mobs_by_name(self, name_like):
        return self.conn.execute(
            "SELECT * FROM mobs WHERE name LIKE ? ORDER BY area_name, name",
            (f"%{name_like}%",)).fetchall()

    def ensure_exit(self, from_vnum, direction):
        """Create a stub exit (to_vnum NULL) if absent; never overwrite
        an existing row - so re-observing a room's exit list is a
        no-op on already-charted edges."""
        self.conn.execute(
            "INSERT OR IGNORE INTO exits (from_vnum, direction) "
            "VALUES (?,?)", (from_vnum, direction))
        self.conn.commit()

    def link_exit(self, from_vnum, direction, to_vnum):
        """Record that `direction` from `from_vnum` leads to `to_vnum`,
        creating the stub first if needed. Only to_vnum is written."""
        self.conn.execute(
            "INSERT OR IGNORE INTO exits (from_vnum, direction) "
            "VALUES (?,?)", (from_vnum, direction))
        self.conn.execute(
            "UPDATE exits SET to_vnum=? "
            "WHERE from_vnum=? AND direction=?",
            (to_vnum, from_vnum, direction))
        self.conn.commit()

    def delete_exit(self, from_vnum, direction):
        """Remove an exit row entirely (a phantom exit a room never had).
        Returns True if a row was deleted."""
        cur = self.conn.execute(
            "DELETE FROM exits WHERE from_vnum=? AND direction=?",
            (from_vnum, direction))
        self.conn.commit()
        return cur.rowcount > 0

    def unlink_exit(self, from_vnum, direction):
        """Re-stub a charted exit: clear its to_vnum (keeping the row) so it
        shows up as an open stub again and Explore re-probes it. Used to
        correct an obvious mischart. Returns True if a row was updated."""
        cur = self.conn.execute(
            "UPDATE exits SET to_vnum=NULL "
            "WHERE from_vnum=? AND direction=?",
            (from_vnum, direction))
        self.conn.commit()
        return cur.rowcount > 0

    def dupe_target_exits(self, area_id):
        """Rooms in an area with two+ charted exits leading to the SAME room
        - an obvious mischart (one room can't normally be reached two
        directions out of a single room). Timed exits (wait_seconds>0) are
        excluded: a cooldown portal legitimately shares a target with a normal
        exit. Returns a list of (from_vnum, to_vnum, [exit_row, ...]) for each
        offending group."""
        rows = self.conn.execute(
            "SELECT e.* FROM exits e JOIN rooms r ON e.from_vnum = r.vnum "
            "WHERE r.area_id = ? AND e.to_vnum IS NOT NULL "
            "AND e.wait_seconds = 0", (area_id,)
        ).fetchall()
        groups = {}
        for row in rows:
            groups.setdefault((row["from_vnum"], row["to_vnum"]), []).append(row)
        return [(fv, tv, g) for (fv, tv), g in groups.items() if len(g) > 1]

    def same_dir_collisions(self, area_id):
        """In-area rooms where 2+ DIFFERENT rooms link to the same target via
        the same direction (e.g. both #4165 and #8630 say n -> #4166). At most
        one can be right - the target can only be one direction away from one
        room - so this is a mischart. Returns
        [(direction, to_vnum, [from_vnum, ...]), ...]. Timed exits excluded."""
        rows = self.conn.execute(
            "SELECT e.from_vnum, e.direction, e.to_vnum FROM exits e "
            "JOIN rooms r ON e.from_vnum = r.vnum "
            "WHERE r.area_id = ? AND e.to_vnum IS NOT NULL "
            "AND e.wait_seconds = 0", (area_id,)).fetchall()
        groups = {}
        for row in rows:
            groups.setdefault((row["direction"], row["to_vnum"]), []).append(
                row["from_vnum"])
        return [(d, t, fl) for (d, t), fl in groups.items() if len(fl) > 1]

    def reverse_contradictions(self, area_id):
        """In-area edges A-d->B where B's reverse exit (opposite of d) is
        charted but points to C != A - the map can't be consistent. Audit
        signal (which of the two edges is wrong is decided elsewhere). Returns
        [(a, d, b, c), ...]. Timed exits excluded both ways."""
        rows = self.conn.execute(
            "SELECT e.from_vnum a, e.direction d, e.to_vnum b FROM exits e "
            "JOIN rooms r ON e.from_vnum = r.vnum "
            "WHERE r.area_id = ? AND e.to_vnum IS NOT NULL "
            "AND e.wait_seconds = 0", (area_id,)).fetchall()
        # index the reverse side: (from, dir) -> to, non-timed only
        fwd = {(r["a"], r["d"]): r["b"] for r in rows}
        out = []
        for r in rows:
            a, d, b = r["a"], r["d"], r["b"]
            rd = REVERSE_DIRS.get(d)
            if rd is None:
                continue
            c = fwd.get((b, rd))
            if c is not None and c != a:
                out.append((a, d, b, c))
        return out

    def wipe_area(self, area_id):
        """Delete every room + exit of an area so it can be re-charted from
        scratch (the area row itself is kept, so a re-chart reuses the same
        area_id/name). Inbound exits from OTHER areas are re-stubbed (to_vnum
        NULL) rather than deleted, so a neighbour's door into the wiped area
        survives as an open stub. Landmarks pointing at wiped rooms are removed.
        Returns (rooms_deleted, inbound_restubbed, landmarks_removed)."""
        rooms = [r["vnum"] for r in self.conn.execute(
            "SELECT vnum FROM rooms WHERE area_id=?", (area_id,))]
        if not rooms:
            return (0, 0, 0)
        ph = ",".join("?" * len(rooms))
        lm = self.conn.execute(
            f"DELETE FROM landmarks WHERE vnum IN ({ph})", rooms).rowcount
        inbound = self.conn.execute(
            f"UPDATE exits SET to_vnum=NULL WHERE to_vnum IN ({ph}) "
            f"AND from_vnum NOT IN ({ph})", rooms + rooms).rowcount
        self.conn.execute(f"DELETE FROM exits WHERE from_vnum IN ({ph})", rooms)
        self.conn.execute(f"DELETE FROM rooms WHERE vnum IN ({ph})", rooms)
        self.conn.commit()
        return (len(rooms), inbound, lm)

    def unrated_frontier(self, area_id):
        """Charted exits FROM in-area rooms whose destination room exists but
        has area_id NULL - a room that was charted but never rated (a capture
        miss, or a border into an overland/other area). Returns
        [(from_vnum, direction, to_vnum), ...]. The caller walks there and
        rates it to fold a genuine in-area hole in (or learn it's foreign)."""
        return self.conn.execute(
            "SELECT e.from_vnum, e.direction, e.to_vnum "
            "FROM exits e JOIN rooms r ON e.from_vnum = r.vnum "
            "JOIN rooms t ON e.to_vnum = t.vnum "
            "WHERE r.area_id = ? AND t.area_id IS NULL "
            "AND e.wait_seconds = 0", (area_id,)).fetchall()

    def has_room(self, vnum):
        return self.get_room(vnum) is not None

    def room_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM rooms").fetchone()[0]

    def exit_dirs(self, from_vnum):
        return {r["direction"] for r in self.conn.execute(
            "SELECT direction FROM exits WHERE from_vnum=?",
            (from_vnum,))}

    # ------------------------------------------------------- readers
    def get_room(self, vnum):
        return self.conn.execute(
            "SELECT * FROM rooms WHERE vnum=?", (vnum,)).fetchone()

    def iter_exits(self, from_vnum):
        return self.conn.execute(
            "SELECT * FROM exits WHERE from_vnum=?",
            (from_vnum,)).fetchall()

    def stubs_in_area(self, area_id):
        """Unresolved exits in an area: known direction, no charted
        destination (to_vnum NULL), non-timed. These are what `Explore`
        walks to and probes to chart the room beyond."""
        return self.conn.execute(
            "SELECT e.* FROM exits e JOIN rooms r ON e.from_vnum = r.vnum "
            "WHERE r.area_id = ? AND e.to_vnum IS NULL "
            "AND e.wait_seconds = 0", (area_id,)).fetchall()

    def neighborhood(self, center, radius=3, same_area_only=True):
        """Lay charted rooms onto a grid around `center` for the map pane,
        following each linked exit's compass direction. `radius` bounds how
        far (in grid cells) the BFS reaches; pass radius=None for the whole
        connected area (the renderer just clips whatever overflows the
        pane). Mirrors
        mapdata.TinMap.neighborhood (the tin renderer) over the sqlite
        schema: BFS from the center, only the current area co-renders
        (spec 6.4), and a residual overlap is flagged 'collide' with
        last-drawn-wins rather than hidden.

        `center` is always placed at (0,0) even if it is not charted yet
        (a brand-new room mid-`rating`), so the caller can still draw the
        'you are here' cell. Only neighbors that are charted rooms reached
        by a linked exit (non-NULL to_vnum) are positioned; stub exits
        (NULL to_vnum) carry no destination and are left for the renderer
        to draw as dangling spokes.

        Returns (grid {(x,y): vnum}, collisions set of (x,y))."""
        c_room = self.get_room(center)
        c_area = c_room["area_id"] if c_room else None
        grid = {(0, 0): center}
        placed = {center: (0, 0)}
        collisions = set()
        q = [center]
        while q:
            rid = q.pop(0)
            x, y = placed[rid]
            for e in self.iter_exits(rid):
                tgt = e["to_vnum"]
                if tgt is None or tgt in placed:
                    continue
                off = GRID_DIRS.get(e["direction"].lower())
                if off is None:                  # u/d/static: not planar
                    continue
                t_room = self.get_room(tgt)
                if t_room is None:               # destination not charted
                    continue
                # Only co-render rooms in the SAME area as the center. A NULL-
                # area neighbour (an overland border, or a not-yet-rated room)
                # is NOT this area, so it's skipped - otherwise it draws on the
                # map AND the BFS follows its exits back into the area, colliding
                # with our own rooms. (When the center itself is overland,
                # c_area is None and the whole filter is off.)
                if same_area_only and c_area is not None \
                        and t_room["area_id"] != c_area:
                    continue
                nx, ny = x + off[0], y + off[1]
                if radius is not None and (abs(nx) > radius
                                           or abs(ny) > radius):
                    continue                 # radius None: whole area
                if (nx, ny) == (0, 0):           # loop back onto center:
                    collisions.add((0, 0))       # flag but keep '@' pinned
                    continue
                if (nx, ny) in grid:             # overlap: flag, don't expand
                    collisions.add((nx, ny))
                    grid[(nx, ny)] = tgt         # last-drawn-wins
                    placed[tgt] = (nx, ny)
                    continue
                grid[(nx, ny)] = tgt
                placed[tgt] = (nx, ny)
                # A foreign-area room reached from an overland center (the
                # filter above let it through for border visibility) is
                # placed so the connection shows, but the BFS must not then
                # walk on INTO that area - otherwise its whole interior (and
                # every one of its open stubs) bleeds onto the overland pane,
                # which is what's actually unexplored ground for THAT area,
                # not this one.
                if c_area is None and t_room["area_id"] is not None:
                    continue
                q.append(tgt)
        return grid, collisions

    def resolve_landmark(self, tag):
        """Resolve a `go <tag>` target to its vnum, or None. Landmarks are
        mud-wide, so the tag alone identifies the room."""
        row = self.conn.execute(
            "SELECT vnum FROM landmarks WHERE tag=?", (tag,)).fetchone()
        return row["vnum"] if row is not None else None


# ==========================================================================
# Speedrun-string generation (spec "Speedrun alias format")
# ==========================================================================
def speedrun_string(edges, letter_for):
    """Render a path (ordered exit rows/dicts) as a speedrun alias BODY
    - no leading separator. `letter_for(direction)` returns the single
    speedrun letter for a canonical direction, or None.

    For each exit: emit `(setup_command)` if set; then, if `command` is
    set and differs from the plain direction, emit `(command)`,
    otherwise emit the direction's letter (a direction with no letter -
    e.g. "shop" - is itself emitted verbatim in parens). Consecutive
    identical letters are run-length compressed: e,e,e -> "3e"."""
    toks = []
    for e in edges:
        direction = e["direction"]
        command = e["command"]
        setup = e["setup_command"]
        if setup:
            toks.append(("v", setup))
        if command and command != direction:
            toks.append(("v", command))
        else:
            ch = letter_for(direction)
            toks.append(("l", ch) if ch else ("v", direction))
    out, i = [], 0
    while i < len(toks):
        kind, val = toks[i]
        if kind == "v":
            out.append(f"({val})")
            i += 1
        else:
            j = i
            while j < len(toks) and toks[j] == ("l", val):
                j += 1
            out.append((str(j - i) if j - i > 1 else "") + val)
            i = j
    return "".join(out)


# ==========================================================================
# Standalone self-test (no test framework in this repo; mirrors the
# tools/ + __main__ pattern). Run: python -m katmud_lib.mapsql --selftest
# ==========================================================================
def _selftest():
    import tempfile

    tmp = tempfile.mkdtemp(prefix="katmud_mapsql_")
    path = os.path.join(tmp, "sub", "3s.db")  # exercises makedirs
    try:
        db = MapDB(path)
        assert os.path.exists(path), "db file not created"
        assert db._user_version() == SCHEMA_VERSION

        db.upsert_area(1, "Cliffs of Vrek", "thoreau")
        db.upsert_room(113129, "On a path to a farm", area_id=1)
        db.upsert_room(113130, "A dusty ledge", area_id=1)
        db.upsert_exit(113129, "e", to_vnum=113130)
        db.upsert_exit(113130, "nw", command="climb over logs",
                       return_command="climb back", wait_seconds=0)

        room = db.get_room(113129)
        assert room["short_desc"] == "On a path to a farm"
        assert room["area_id"] == 1
        exits = db.iter_exits(113130)
        assert len(exits) == 1 and exits[0]["command"] == "climb over logs"

        # composite-key upsert replaces, not duplicates
        db.upsert_room(113131, "A second farm path", area_id=1)
        db.upsert_exit(113129, "e", to_vnum=113131)
        assert len(db.iter_exits(113129)) == 1
        assert db.iter_exits(113129)[0]["to_vnum"] == 113131

        # FK is enforced: an exit to an uncharted room is rejected
        try:
            db.upsert_exit(113129, "w", to_vnum=999999)
            raise AssertionError("FK not enforced on exits.to_vnum")
        except sqlite3.IntegrityError:
            pass

        # landmarks are mud-wide: one tag -> one room, re-add overwrites
        db.add_landmark("bank", 113129)
        assert db.resolve_landmark("bank") == 113129
        db.add_landmark("bank", 113130)          # same tag re-points
        assert db.resolve_landmark("bank") == 113130
        db.add_landmark("bank", 113129)          # restore for later asserts
        assert db.resolve_landmark("nosuch") is None

        # charting helpers: area get-or-create is idempotent by name
        aid = db.get_or_create_area("Cliffs of Vrek", "thoreau")
        assert aid == 1
        assert db.get_or_create_area("Cliffs of Vrek") == 1
        nid = db.get_or_create_area("New Area")
        assert nid != aid
        db.set_room_area(113130, nid)
        assert db.get_room(113130)["area_id"] == nid

        # room notes round-trip (v2 column); charting must not wipe them
        db.set_room_note(113130, "guildmaster here")
        assert db.get_room(113130)["notes"] == "guildmaster here"
        db.upsert_room(113130, "A dusty ledge", area_id=nid)  # re-chart
        assert db.get_room(113130)["notes"] == "guildmaster here"
        db.set_room_note(113130, None)
        assert db.get_room(113130)["notes"] is None

        # ensure_exit stubs without clobbering; link_exit fills to_vnum
        # but preserves a special exit's command (mapfix) columns
        db.upsert_exit(113130, "nw", command="climb over logs",
                       return_command="climb back")
        db.ensure_exit(113130, "nw")          # must NOT wipe command
        db.link_exit(113130, "nw", 113129)    # only sets to_vnum
        ex = {r["direction"]: r for r in db.iter_exits(113130)}
        assert ex["nw"]["command"] == "climb over logs"
        assert ex["nw"]["return_command"] == "climb back"
        assert ex["nw"]["to_vnum"] == 113129
        db.ensure_exit(113130, "n")           # fresh stub
        ex_dirs = db.exit_dirs(113130)
        assert "n" in ex_dirs and db.has_room(113130)
        assert db.room_count() == 3

        # exit_to finds the direction whose far side is a given room
        assert db.exit_to(113130, 113129)["direction"] == "nw"
        assert db.exit_to(113130, 999999) is None

        # landmarks_for lists the mud-wide namespace; remove_landmark deletes
        vis = db.landmarks_for()
        assert sorted(r["tag"] for r in vis) == ["bank"], vis
        assert all(r["scope"] == "mud" for r in vis), vis
        db.remove_landmark("bank")
        assert db.resolve_landmark("bank") is None
        assert db.landmarks_for() == []

        # --- pathfinding + speedrun generation ---
        for v in (1, 2, 3, 4, 5, 6, 7):
            db.upsert_room(v, f"room {v}")
        db.link_exit(1, "e", 2)
        db.link_exit(2, "e", 3)
        db.link_exit(3, "e", 4)
        db.link_exit(4, "shop", 5)          # non-compass: verbatim token
        db.link_exit(5, "ne", 6)            # diagonal: letter via map
        # a timed shortcut 1->4 that pathfinding must NOT use
        db.upsert_exit(1, "d", to_vnum=4, wait_seconds=10)

        rev = {"e": "e", "ne": "t", "d": "d"}.get   # direction -> letter
        p = db.find_path(1, 4)
        assert [e["direction"] for e in p] == ["e", "e", "e"], p
        assert speedrun_string(p, rev) == "3e"
        p2 = db.find_path(1, 6)
        assert speedrun_string(p2, rev) == "3e(shop)t", \
            speedrun_string(p2, rev)
        assert db.find_path(1, 1) == []
        assert db.find_path(1, 7) is None    # isolated room: no path
        # a special (mapfix) exit renders setup + command verbatim
        edge = {"direction": "nw", "command": "climb over logs",
                "setup_command": "unlock door"}
        assert speedrun_string([edge], rev) == "(unlock door)(climb over logs)"

        # --- mapfix: set_exit_field + reverse routing via return_command ---
        db.upsert_room(10, "ledge top")
        db.upsert_room(11, "ledge bottom")
        db.link_exit(10, "nw", 11)
        db.set_exit_field(10, "nw", "command", "climb over logs")
        db.set_exit_field(10, "nw", "return_command", "climb back up")
        e = db.get_exit(10, "nw")
        assert e["command"] == "climb over logs"
        assert e["return_command"] == "climb back up"
        # forward path uses the special command
        assert speedrun_string(db.find_path(10, 11), rev) \
            == "(climb over logs)"
        # reverse path is routable via the return_command (no real
        # 11->10 edge exists, only the forward exit's return_command)
        assert db.get_exit(11, "se") is None
        assert speedrun_string(db.find_path(11, 10), rev) \
            == "(climb back up)"
        # set_exit_field creates a stub for an exit the MUD never listed
        db.set_exit_field(10, "climb", "command", "scramble up")
        assert db.get_exit(10, "climb")["command"] == "scramble up"
        # a timed exit is excluded both ways (wait sits on the one row)
        db.set_exit_field(10, "nw", "wait_seconds", 30)
        assert db.find_path(10, 11) is None
        assert db.find_path(11, 10) is None
        db.set_exit_field(10, "nw", "wait_seconds", 0)
        assert db.find_path(10, 11) is not None

        try:
            db.set_exit_field(10, "nw", "vnum", 5)   # not whitelisted
            raise AssertionError("set_exit_field allowed a bad column")
        except ValueError:
            pass

        # --- neighborhood: BFS layout for the map pane ---
        for v in (20, 21, 22, 23, 24, 25):
            db.upsert_room(v, f"grid {v}")
        db.link_exit(20, "e", 21)            # 21 at (1,0)
        db.link_exit(20, "n", 22)            # 22 at (0,-1)
        db.link_exit(21, "n", 23)            # 23 at (1,-1)
        db.link_exit(22, "e", 24)            # 24 also wants (1,-1): collide
        db.link_exit(21, "w", 25)            # 25 loops back onto center
        db.ensure_exit(20, "w")              # stub (NULL to_vnum)
        grid, coll = db.neighborhood(20, radius=3)
        assert grid[(0, 0)] == 20 and grid[(1, 0)] == 21  # center pinned
        assert grid[(0, -1)] == 22
        assert 25 not in grid.values(), "room must not clobber the center"
        assert coll == {(1, -1), (0, 0)}, coll   # cell overlaps, incl center
        assert (-1, 0) not in grid, "stub 'w' must not place a room"
        # area scoping: from a real-area center, only same-area neighbours draw -
        # a NULL-area (overland border) or different-area neighbour is skipped,
        # so its exits can't be followed back into our area.
        other = db.get_or_create_area("Next Door")
        db.upsert_room(100, "hub", area_id=1)
        db.upsert_room(101, "east room", area_id=1)
        db.upsert_room(102, "overland border")          # area_id NULL
        db.upsert_room(103, "foreign", area_id=other)
        db.link_exit(100, "e", 101)
        db.link_exit(100, "n", 102)        # overland - must NOT draw
        db.link_exit(100, "w", 103)        # different area - must NOT draw
        db.link_exit(102, "s", 100)        # the back-exit that used to collide
        gscope, _ = db.neighborhood(100, radius=3)
        assert gscope.get((1, 0)) == 101, gscope
        assert 102 not in gscope.values(), "overland border must not draw"
        assert 103 not in gscope.values(), "foreign room must not draw"
        # center is always placed, even for an uncharted current room
        g2, c2 = db.neighborhood(987654)
        assert g2 == {(0, 0): 987654} and c2 == set()
        # radius clamps how far the BFS reaches
        gr, _ = db.neighborhood(20, radius=0)
        assert gr == {(0, 0): 20}, gr
        # radius=None lays out the whole connected area (no clamp)
        for vv in (60, 61, 62, 63, 64):
            db.upsert_room(vv, f"line {vv}", area_id=1)
        db.link_exit(60, "e", 61)
        db.link_exit(61, "e", 62)
        db.link_exit(62, "e", 63)
        db.link_exit(63, "e", 64)
        gall, _ = db.neighborhood(60, radius=None)
        assert gall.get((4, 0)) == 64, gall   # 4 cells east, past radius 3
        gclamp, _ = db.neighborhood(60, radius=3)
        assert (4, 0) not in gclamp

        # --- dupe_target_exits + unlink_exit: detect & re-stub a mischart ---
        db.upsert_room(70, "junction", area_id=1)
        db.upsert_room(71, "vault", area_id=1)
        db.link_exit(70, "n", 71)
        db.link_exit(70, "w", 71)            # mischart: n & w both -> 71
        db.link_exit(70, "e", 60)            # a normal, non-dup exit
        dupes = db.dupe_target_exits(1)
        assert len(dupes) == 1, dupes
        fv, tv, drows = dupes[0]
        assert fv == 70 and tv == 71, (fv, tv)
        assert sorted(r["direction"] for r in drows) == ["n", "w"], drows
        # re-stub one side: its to_vnum clears (row kept) and the dup resolves
        assert db.unlink_exit(70, "w") is True
        assert db.get_exit(70, "w")["to_vnum"] is None
        assert db.dupe_target_exits(1) == []
        assert any(s["from_vnum"] == 70 and s["direction"] == "w"
                   for s in db.stubs_in_area(1)), "re-stubbed exit must show"
        assert db.unlink_exit(70, "u") is False  # no such exit row

        # --- maze-mischart detectors: same_dir_collisions / reverse /
        #     unrated_frontier (mirrors the Ruins of Xylogth bugs) ---
        for v in (80, 81, 82):
            db.upsert_room(v, f"maze {v}", area_id=1)
        # both 80 and 81 claim n -> 82; 82's reverse (s) confirms 81 is right,
        # so 80's n is the loser to re-stub.
        db.link_exit(80, "n", 82)
        db.link_exit(81, "n", 82)
        db.link_exit(82, "s", 81)
        coll = db.same_dir_collisions(1)
        coll = [c for c in coll if c[1] == 82]
        assert len(coll) == 1, coll
        d, t, froms = coll[0]
        assert d == "n" and t == 82 and sorted(froms) == [80, 81], coll
        # reverse_contradictions sees 80-n->82 vs 82-s->81
        rc = [x for x in db.reverse_contradictions(1) if x[2] == 82]
        assert (80, "n", 82, 81) in rc, rc
        # unrated_frontier: 82-d-> an existing but unrated room
        db.upsert_room(83, "unrated hole")        # area_id stays NULL
        db.link_exit(82, "d", 83)
        fr = [tuple(r) if not hasattr(r, "keys") else
              (r["from_vnum"], r["direction"], r["to_vnum"])
              for r in db.unrated_frontier(1)]
        assert (82, "d", 83) in fr, fr

        # --- wipe_area: delete an area, re-stub inbound, keep the area row ---
        wipe_aid = db.get_or_create_area("Wipe Me")
        db.upsert_room(90, "wipe a", area_id=wipe_aid)
        db.upsert_room(91, "wipe b", area_id=wipe_aid)
        db.upsert_room(92, "neighbour", area_id=1)   # different area
        db.link_exit(90, "e", 91)                    # internal edge
        db.link_exit(92, "n", 90)                    # inbound from area 1
        db.add_landmark("wipespot", 91)
        rd, inb, lmd = db.wipe_area(wipe_aid)
        assert (rd, inb, lmd) == (2, 1, 1), (rd, inb, lmd)
        assert db.get_room(90) is None and db.get_room(91) is None
        assert db.get_exit(92, "n")["to_vnum"] is None  # inbound re-stubbed
        assert db.resolve_landmark("wipespot") is None  # landmark removed
        assert db.get_or_create_area("Wipe Me") == wipe_aid  # area row kept
        assert db.wipe_area(wipe_aid) == (0, 0, 0)   # idempotent on empty
        db.close()

        # reopening an existing db is a no-op (idempotent schema)
        db2 = MapDB(path)
        assert db2.get_room(113129)["short_desc"] == "On a path to a farm"
        assert db2._user_version() == SCHEMA_VERSION
        db2.close()

        # a pre-notes (v1) database gains the column on open, keeping data
        v1 = os.path.join(tmp, "v1.db")
        c = sqlite3.connect(v1)
        c.executescript(
            "CREATE TABLE rooms (vnum INTEGER PRIMARY KEY, short_desc TEXT,"
            " area_id INTEGER, x INTEGER, y INTEGER, z INTEGER);"
            "PRAGMA user_version=1;")
        c.execute("INSERT INTO rooms (vnum, short_desc) VALUES (1,'old')")
        c.commit()
        c.close()
        dbm = MapDB(v1)
        assert dbm._user_version() == SCHEMA_VERSION
        assert "notes" in {r["name"] for r in
                           dbm.conn.execute("PRAGMA table_info(rooms)")}
        assert dbm.get_room(1)["short_desc"] == "old"   # data preserved
        dbm.set_room_note(1, "added later")
        assert dbm.get_room(1)["notes"] == "added later"
        dbm.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("OK")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: python -m katmud_lib.mapsql --selftest")
