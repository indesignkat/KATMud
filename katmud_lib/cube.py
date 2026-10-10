"""Explorer for the masked-exit cubes in a section of hell (3s and 3k).

Exits there are named thisway/thatway/anotherway/overhere/overthere/yonder
instead of directions, and the mapping is PER ROOM - 22197 -yonder-> 22198
comes back by `overhere`, but 22202 -yonder-> 22203 comes back by
`overthere` - reshuffled weekly. Decoding the masks gains nothing; the room
numbers are honest (verified by dropping a rosary and retracing), so this
maps by number and treats each room's masks as plain labels.

GMCP Room.Info lists every exit; the mud fills in the destination of the
ones you have walked FROM that room and sends 0 for the rest. That is the
whole map: {room: {mask: destination, 0 = unexplored, -1 = refused}}.
"""
from collections import deque

MASKS = frozenset(("thisway", "thatway", "anotherway",
                   "overhere", "overthere", "yonder"))


def is_masked(exits):
    """True for a room whose exits use the mask words."""
    return any(k in MASKS for k in exits)


# The 6th plane of hell hides its exits: Room.Info lists {} (or only
# {"portal": 0}), and a wrong direction leaves you where you were with no
# Room.Info at all - just a Room.Contents (logs/gmcp_20261010_014607.log).
PROBE_DIRS = ("n", "s", "e", "w")


def walk_exits(exits):
    """The exits the walker may try from a room: a masked room's own list;
    otherwise what Room.Info lists plus the cardinal directions to probe.
    Never `portal` - stepping through one is left to the player."""
    if is_masked(exits):
        return dict(exits)
    out = {k: v for k, v in exits.items() if k != "portal"}
    for d in PROBE_DIRS:
        out.setdefault(d, 0)
    return out


class CubeMap:
    def __init__(self, rooms=None, portals=None):
        self.rooms = rooms or {}
        # Rooms with a `portal` exit. Kept through a reshuffle: the masks
        # move, the rooms do not.
        self.portals = set(portals or ())

    def _reshuffled(self):
        self.rooms = {}

    def observe(self, num, exits):
        """A Room.Info for `num`. Merges in what the mud says it knows.
        Returns True if this contradicts the saved map - the weekly
        reshuffle - in which case the old edges are dropped first."""
        old = self.rooms.get(num)
        reset = False
        if old is not None:
            if set(old) != set(exits) or any(
                    v and old.get(k) not in (0, v) for k, v in exits.items()):
                self._reshuffled()
                old, reset = None, True
        merged = {k: (v or (old or {}).get(k, 0)) for k, v in exits.items()}
        self.rooms[num] = merged
        return reset

    def learn(self, frm, mask, to):
        """We sent `mask` in `frm` and arrived in `to`. Returns True if a
        known exit led somewhere else (reshuffle): the map restarts from
        this one proven edge."""
        known = self.rooms.get(frm, {}).get(mask)
        if known and known > 0 and known != to:
            self._reshuffled()
            self.rooms[frm] = {mask: to}
            return True
        self.rooms.setdefault(frm, {})[mask] = to
        return False

    def dead(self, num, mask):
        """The mud refused `mask` here ("There is no reason to ...").
        Kept as -1 rather than deleted: Room.Info will keep listing it."""
        if mask in self.rooms.get(num, {}):
            self.rooms[num][mask] = -1

    def forget_dead_ends(self):
        """Mark every exit that went nowhere (a self-loop) or was refused
        as unexplored again; returns how many. The 9th plane's speedwalk
        took `s` from 38477 after Cube had recorded it as going nowhere."""
        n = 0
        for r, ex in self.rooms.items():
            for k, v in ex.items():
                if v == r or v == -1:
                    ex[k] = 0
                    n += 1
        return n

    def _maybe_new(self, room):
        """Could one of `room`'s unexplored exits reach a room we have not
        seen? Rooms known to lead INTO it, that it has no known exit back
        to, account for that many of its unexplored exits already."""
        ex = self.rooms.get(room, {})
        unknown = sum(1 for v in ex.values() if v == 0)
        back = set(v for v in ex.values() if v > 0)
        into = {r for r, e in self.rooms.items()
                if r != room and room in e.values() and r not in back}
        return unknown > len(into)

    def plan(self, cur):
        """The next mask to send from `cur` and why, or None when nothing
        is left to explore. Walks known exits (breadth-first, so the
        nearest first) to a room with an unexplored exit, preferring one
        whose unexplored exits cannot all lead back to known rooms."""
        first = {cur: None}             # room -> first mask on the path
        order = [cur]
        q = deque([cur])
        while q:
            r = q.popleft()
            for mask, to in sorted(self.rooms.get(r, {}).items()):
                if to > 0 and to not in first:
                    first[to] = first[r] or mask
                    order.append(to)
                    q.append(to)
        rooms = [r for r in order
                 if any(v == 0 for v in self.rooms.get(r, {}).values())]
        if not rooms:
            return None
        target = next((r for r in rooms if self._maybe_new(r)), rooms[0])
        if target == cur:
            mask = sorted(k for k, v in self.rooms[cur].items() if v == 0)[0]
            return mask, "unexplored exit"
        return first[target], f"to {target}"

    def tour(self, cur, seen):
        """First mask toward the nearest room reachable by known exits that
        is not in `seen`, and that room; None when every reachable room has
        been seen. For a search with nothing left to explore."""
        first = {cur: None}
        q = deque([cur])
        while q:
            r = q.popleft()
            if r not in seen:
                return first[r], r
            for mask, to in sorted(self.rooms.get(r, {}).items()):
                if to > 0 and to not in first:
                    first[to] = first[r] or mask
                    q.append(to)
        return None

    def route(self, cur, goal):
        """The masks that walk known exits from `cur` to `goal` (shortest
        by steps), [] when already there, None when the map has no way."""
        prev = {cur: None}
        q = deque([cur])
        while q:
            r = q.popleft()
            if r == goal:
                break
            for mask, to in sorted(self.rooms.get(r, {}).items()):
                if to > 0 and to not in prev:
                    prev[to] = (r, mask)
                    q.append(to)
        if goal not in prev:
            return None
        path = []
        while prev[goal]:
            goal, mask = prev[goal]
            path.append(mask)
        return path[::-1]

    def to_json(self):
        out = {str(r): ex for r, ex in self.rooms.items()}
        out["portals"] = sorted(self.portals)
        return out

    @classmethod
    def from_json(cls, data):
        data = dict(data or {})
        portals = data.pop("portals", None) or []
        rooms = {}
        for r, ex in data.items():
            try:
                rooms[int(r)] = {str(k): int(v) for k, v in ex.items()}
            except (TypeError, ValueError, AttributeError):
                continue
        return cls(rooms, (int(p) for p in portals
                           if isinstance(p, int)))
