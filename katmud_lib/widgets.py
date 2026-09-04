"""katmud_lib.widgets - small custom Tk widgets: vitals bars and the
map pane. Ported from v6; the map pane gains the v7 area header
(spec 6.5: '<Area> [<Coder>]' + room name - permanent facts only)
and visible collision markers (6.4: punt visibly, not confusingly).
"""

import tkinter as tk


class VitalsBar(tk.Canvas):
    """One labelled gradient bar: name, current/max, colored fill."""

    def __init__(self, parent, label, color, width=190, font=None,
                 warn=None, **kw):
        super().__init__(parent, width=width, height=22, bg="#15151c",
                         highlightthickness=0, **kw)
        self.label = label
        self.color = color
        self.w = width
        self.h = 22
        self.warn = warn or []
        self.font = font or ("Consolas", 9, "bold")
        self.set(0, 0)

    def set(self, cur, top):
        self.delete("all")
        h = getattr(self, "h", 22)
        frac = (cur / top) if top else 0.0
        frac = max(0.0, min(1.0, frac))
        self.create_rectangle(0, 0, self.w, h, fill="#22222c", width=0)
        fill = self.color
        if top:
            for threshold, color in sorted(self.warn):
                if frac < threshold:
                    fill = color
                    break
        self.create_rectangle(0, 0, int(self.w * frac), h,
                              fill=fill, width=0)
        self.create_text(6, h // 2, anchor="w", fill="#ffffff",
                         font=self.font,
                         text=f"{self.label} {cur}/{top}")


class MapPane(tk.Canvas):
    """Neighborhood map. Two modes:
      graph mode  - tt++ rooms on a grid (current area only), click
                    adjacent to step, farther to speedwalk
      compass mode - fallback spokes from live MIP exits when not
                    located on the map
    Header: '<Area> [<Coder>]' + room name (spec 6.5)."""

    DIRS = {
        "n": (0, -1), "s": (0, 1), "e": (1, 0), "w": (-1, 0),
        "ne": (1, -1), "nw": (-1, -1), "se": (1, 1), "sw": (-1, 1),
    }

    def __init__(self, parent, send_cb, walk_cb=None, fonts=None, **kw):
        super().__init__(parent, bg="#10101a", highlightthickness=0, **kw)
        self.send_cb = send_cb
        self.walk_cb = walk_cb
        self.fonts = fonts or {}
        self.room = "?"
        self.exits = []
        self.trail = []
        self.graph = None      # {(x,y): {rid, exits, here, collide, house}}
        self.area = ""
        self.coder = ""
        self.gstatus = ""
        self.mapping = False    # mapping-mode banner
        self.bind("<Configure>", lambda e: self.redraw())

    def set_graph(self, payload, area="", coder="", status="",
                  mapping=False):
        self.graph = payload
        self.area = area
        self.coder = coder
        self.gstatus = status
        self.mapping = mapping
        self.redraw()

    def f(self, which, fallback):
        return self.fonts.get(which, fallback)

    def update_room(self, room=None, exits=None):
        if room is not None and room != self.room:
            self.trail.append(room)
            self.trail = self.trail[-6:]
            self.room = room
        if exits is not None:
            self.exits = exits
        self.redraw()

    def _header(self, w):
        hdr = self.room
        sub = ""
        if self.area:
            sub = self.area + (f" [{self.coder}]" if self.coder else "")
        if sub:
            self.create_text(w // 2, 10, text=sub, fill="#9999cc",
                             font=self.f("trail", ("Consolas", 8)))
            self.create_text(w // 2, 24, text=hdr, fill="#ccccff",
                             font=self.f("room",
                                         ("Consolas", 10, "bold")))
        else:
            # No area (the viking biome clears the graph, so nothing
            # names the area): the room is the only header line, and it
            # gets the room font. It used to be drawn small here and
            # BOLD again by the compass path four pixels lower, which is
            # the shadowed title reported on 2026-09-04.
            self.create_text(w // 2, 12, text=hdr, fill="#ccccff",
                             font=self.f("room",
                                         ("Consolas", 10, "bold")))
        if self.mapping:
            self.create_text(w - 6, 10, anchor="e", text="MAPPING",
                             fill="#ffaa44",
                             font=self.f("trail", ("Consolas", 8)))

    def draw_graph(self, w, h):
        cell = max(22, min(40, (min(w, h) - 40) // 7))
        cx, cy = w // 2, h // 2 + 8
        half = cell * 0.32
        for (x, y), info in self.graph.items():
            px, py = cx + x * cell, cy + y * cell
            stubs = info.get("stubs") or ()
            links = info.get("links") or {}
            for d in info["exits"]:
                off = self.DIRS.get(d.lower())
                if not off:
                    continue
                tx, ty = x + off[0], y + off[1]
                nb = self.graph.get((tx, ty))
                if nb is not None and links.get(d) == nb["rid"]:
                    # a real charted link to the room actually drawn there
                    self.create_line(px, py, px + off[0] * cell,
                                     py + off[1] * cell,
                                     fill="#445566", width=2)
                elif d.lower() in stubs and nb is None:
                    # known exit, destination not mapped yet: a short
                    # dashed spoke pointing the way, with no room box.
                    # Suppressed when that cell already holds a room - the
                    # spoke would touch it and read as a connection, which
                    # is exactly how 1191's `sw` looked joined to 272.
                    self.create_line(px, py, px + off[0] * cell * 0.5,
                                     py + off[1] * cell * 0.5,
                                     fill="#665544", width=1, dash=(3, 3))
        for (x, y), info in self.graph.items():
            px, py = cx + x * cell, cy + y * cell
            here = info["here"]
            tag = f"g_{x}_{y}"
            fill = "#5555aa" if here else "#223344"
            line = "#aaaaff" if here else "#557788"
            if info.get("house"):
                fill = "#446644" if not here else "#55aa55"
            if info.get("collide"):
                line = "#cc8833"          # visible overlap marker
            if info.get("new"):
                # current room not charted yet (pending rating / in house)
                fill = "#665522" if here else "#332a11"
                line = "#ffaa44"
            self.create_rectangle(px - half, py - half, px + half,
                                  py + half, fill=fill, outline=line,
                                  width=2 if here or info.get("collide")
                                  else 1, tags=(tag,))
            ud = "".join(c for d in info["exits"]
                         for c in ("\u25b4" if d.lower() in ("u", "up")
                                   else "\u25be" if d.lower()
                                   in ("d", "down") else ""))
            label = "@" if here else ("!" if info.get("collide")
                                      else (ud or ""))
            if label:
                self.create_text(px, py, text=label, fill="#ffffff"
                                 if here else "#88ccaa",
                                 font=self.f("exit",
                                             ("Consolas", 9, "bold")),
                                 tags=(tag,))
            if not here:
                if max(abs(x), abs(y)) == 1:
                    for d, off in self.DIRS.items():
                        if off == (x, y):
                            self.tag_bind(tag, "<Button-1>",
                                          lambda _e, dd=d:
                                          self.send_cb(dd))
                            break
                elif self.walk_cb:
                    self.tag_bind(tag, "<Button-1>",
                                  lambda _e, r=info["rid"]:
                                  self.walk_cb(r))
        self._header(w)
        if self.gstatus:
            self.create_text(6, h - 10, anchor="w", text=self.gstatus,
                             fill="#555577",
                             font=self.f("trail", ("Consolas", 8)))

    def redraw(self):
        self.delete("all")
        # Charting (map-building) mode tints the whole pane warm amber so
        # the mode - in which input is gated one move at a time - is
        # unmistakable; following mode is the cool default.
        self.configure(bg="#241a08" if self.mapping else "#10101a")
        w = self.winfo_width() or 220
        h = self.winfo_height() or 220
        if self.graph:
            self.draw_graph(w, h)
            return
        cx, cy = w // 2, h // 2 - 8
        r = min(w, h) // 2 - 36
        if r < 30:
            return
        for d in self.exits:
            dl = d.lower()
            if dl in self.DIRS:
                dx, dy = self.DIRS[dl]
                norm = (dx * dx + dy * dy) ** 0.5
                ex = cx + int(dx / norm * r)
                ey = cy + int(dy / norm * r)
                self.create_line(cx, cy, ex, ey, fill="#557755", width=3,
                                 tags=(f"exit_{dl}",))
                self.create_oval(ex - 11, ey - 11, ex + 11, ey + 11,
                                 fill="#224422", outline="#66aa66",
                                 tags=(f"exit_{dl}",))
                self.create_text(ex, ey, text=dl, fill="#aaffaa",
                                 font=self.f("exit",
                                             ("Consolas", 8, "bold")),
                                 tags=(f"exit_{dl}",))
                self.tag_bind(f"exit_{dl}", "<Button-1>",
                              lambda _e, dd=dl: self.send_cb(dd))
        odd = [d for d in self.exits if d.lower() not in self.DIRS]
        if odd:
            # Above the trail block, not at cy + r + 18: for a pane wider
            # than it is tall that reduces to exactly h - 26, which is
            # where the trail's SECOND line goes - so '[up]' was printed
            # over a room name (reported with the shadowed title,
            # 2026-09-04). The trail grows upward from h - 12.
            # trail[:-1] is what gets drawn, so n-1 gaps below this row.
            drawn = max(0, len(self.trail) - 1)
            y = h - 12 - 14 * max(0, drawn - 1) - 16
            x = 10
            for d in odd:
                t = self.create_text(x, y, anchor="w", text=f"[{d}]",
                                     fill="#aaffaa",
                                     font=self.f("exit",
                                                 ("Consolas", 9,
                                                  "bold")),
                                     tags=(f"odd_{d}",))
                bbox = self.bbox(t)
                self.tag_bind(f"odd_{d}", "<Button-1>",
                              lambda _e, dd=d: self.send_cb(dd))
                x = bbox[2] + 10
        self.create_oval(cx - 16, cy - 16, cx + 16, cy + 16,
                         fill="#333355", outline="#7777cc", width=2)
        self._header(w)
        for i, name in enumerate(reversed(self.trail[:-1])):
            self.create_text(8, h - 12 - i * 14, anchor="w",
                             text=name, fill="#555577",
                             font=self.f("trail", ("Consolas", 8)))


def add_window_menu(win, topmost=False):
    """Give a detached panel a Window menu with an Always on Top toggle.

    Shared rather than copied: every status panel wants it, and the next
    one should get it by calling this instead of by remembering to
    reimplement it. Returns the BooleanVar so the caller can persist the
    choice (the windows save it beside their geometry on close).

    -topmost is per-window, so one panel pinned does not pin the rest.
    """
    var = tk.BooleanVar(master=win, value=bool(topmost))

    def toggle():
        try:
            win.attributes("-topmost", bool(var.get()))
        except tk.TclError:
            pass        # window closing, or a platform without it

    # tearoff=0 on BOTH: a tearoff entry sits at index 0 and shifts every
    # real entry down one, which is confusing to drive and ugly besides.
    bar = tk.Menu(win, tearoff=0)
    menu = tk.Menu(bar, tearoff=0)
    menu.add_checkbutton(label="Always on Top", variable=var,
                         command=toggle)
    bar.add_cascade(label="Window", menu=menu)
    try:
        win.configure(menu=bar)
    except tk.TclError:
        return var
    if var.get():
        toggle()
    win.topmost_var = var
    return var
