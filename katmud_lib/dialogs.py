"""katmud_lib.dialogs - the scoped builder (spec section 4), the
keybindings dialog (4.1), and the font dialog.

Builder rules:
  * scope selector on every save, labeled concretely ("3s / vikings")
  * default scope: character (narrowest - wrong-too-narrow is an
    annoyance; wrong-too-broad fires untested automation elsewhere)
  * guild grayed out when profile guild = none
  * Move-to-scope action for one-click promotion
"""

import re
import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont

from . import config

BG = "#1a1a1a"
FG = "#ccccdd"
FIELD_BG = "#101018"
ENTRY_BG = "#1e1e26"
SEL_BG = "#334466"


class ScopeBar(tk.Frame):
    """Radio row: character / guild / mud / global with concrete
    labels. Default = character."""

    def __init__(self, parent, client, **kw):
        super().__init__(parent, bg=BG, **kw)
        self.client = client
        self.var = tk.StringVar(value="character")
        tk.Label(self, text="Save to:", bg=BG, fg=FG).pack(side="left",
                                                           padx=(0, 6))
        for scope in ("character", "guild", "mud", "global"):
            label = client.cascade.label_for(scope) \
                if not (scope == "guild"
                        and client.guild.lower() == "none") \
                else "(no guild)"
            rb = tk.Radiobutton(
                self, text=label, value=scope, variable=self.var,
                bg=BG, fg=FG, selectcolor=FIELD_BG,
                activebackground=BG, activeforeground=FG)
            if scope == "guild" and client.guild.lower() == "none":
                rb.configure(state="disabled")
            rb.pack(side="left", padx=4)

    @property
    def scope(self):
        return self.var.get()


class BuilderDialog(tk.Toplevel):
    """Aliases & triggers across all cascade layers, with per-entry
    scope shown, scope selector on save, and move-to-scope."""

    def __init__(self, client, prefill_pattern=None, sample_line=None):
        super().__init__(client.root)
        self.c = client
        self.title("Aliases & Triggers (cascade)")
        self.configure(bg=BG)
        self.geometry("860x560")
        self.transient(client.root)

        def mklabel(parent, txt):
            tk.Label(parent, text=txt, bg=BG, fg=FG).pack(
                anchor="w", padx=8, pady=(8, 2))

        # ---------- aliases (left) ----------
        left = tk.Frame(self, bg=BG)
        mklabel(left, "Aliases   (scope shown per entry)")
        self.a_lb = tk.Listbox(left, bg=FIELD_BG, fg=FG,
                               selectbackground=SEL_BG,
                               exportselection=False)
        self.a_lb.pack(fill="both", expand=True, padx=8)
        self.a_names = []

        self.a_name = tk.Entry(left, bg=ENTRY_BG, fg="#eeeeee",
                               insertbackground="#eeeeee")
        self.a_body = tk.Entry(left, bg=ENTRY_BG, fg="#eeeeee",
                               insertbackground="#eeeeee")
        mklabel(left, "Name:")
        self.a_name.pack(fill="x", padx=8)
        mklabel(left, "Commands (%1..%9, %*):")
        self.a_body.pack(fill="x", padx=8)
        self.a_scope = ScopeBar(left, client)
        self.a_scope.pack(anchor="w", padx=8, pady=4)
        ab = tk.Frame(left, bg=BG)
        tk.Button(ab, text="Save", command=self.a_save, bg=SEL_BG,
                  fg="#ffffff").pack(side="left", padx=4)
        tk.Button(ab, text="Delete", command=self.a_delete,
                  bg="#663333", fg="#ffffff").pack(side="left", padx=4)
        tk.Button(ab, text="Move to scope", command=self.a_move,
                  bg="#556633", fg="#ffffff").pack(side="left", padx=4)
        ab.pack(pady=8)
        self.a_lb.bind("<<ListboxSelect>>", self.a_pick)

        # ---------- triggers (right) ----------
        right = tk.Frame(self, bg=BG)
        mklabel(right, "Triggers")
        self.t_lb = tk.Listbox(right, bg=FIELD_BG, fg=FG,
                               selectbackground=SEL_BG,
                               exportselection=False)
        self.t_lb.pack(fill="both", expand=True, padx=8)
        self.t_patterns = []

        mklabel(right, "Pattern (regex):")
        self.t_pat = tk.Entry(right, bg=ENTRY_BG, fg="#eeeeee",
                              insertbackground="#eeeeee")
        self.t_pat.pack(fill="x", padx=8)
        mklabel(right, "Commands (\\1..\\9 for groups):")
        self.t_cmd = tk.Entry(right, bg=ENTRY_BG, fg="#eeeeee",
                              insertbackground="#eeeeee")
        self.t_cmd.pack(fill="x", padx=8)
        mklabel(right, "Sound (.wav path, 'beep', or blank):")
        sndrow = tk.Frame(right, bg=BG)
        self.t_snd = tk.Entry(sndrow, bg=ENTRY_BG, fg="#eeeeee",
                              insertbackground="#eeeeee")
        self.t_snd.pack(side="left", fill="x", expand=True)
        tk.Button(sndrow, text="...", command=self.browse_snd,
                  bg=SEL_BG, fg="#ffffff", width=3).pack(side="left",
                                                         padx=4)
        sndrow.pack(fill="x", padx=8)
        mklabel(right, "Test against sample line:")
        self.t_test = tk.Entry(right, bg=ENTRY_BG, fg="#eeeeee",
                               insertbackground="#eeeeee")
        self.t_test.pack(fill="x", padx=8)
        self.t_result = tk.Label(right, text="", bg=BG, fg="#8899aa",
                                 anchor="w", justify="left")
        self.t_result.pack(fill="x", padx=8, pady=4)
        for w in (self.t_pat, self.t_test):
            w.bind("<KeyRelease>", self.t_check)
        self.t_scope = ScopeBar(right, client)
        self.t_scope.pack(anchor="w", padx=8, pady=4)
        tb = tk.Frame(right, bg=BG)
        tk.Button(tb, text="Save", command=self.t_save, bg=SEL_BG,
                  fg="#ffffff").pack(side="left", padx=4)
        tk.Button(tb, text="Delete", command=self.t_delete,
                  bg="#663333", fg="#ffffff").pack(side="left", padx=4)
        tk.Button(tb, text="Move to scope", command=self.t_move,
                  bg="#556633", fg="#ffffff").pack(side="left", padx=4)
        tb.pack(pady=8)
        self.t_lb.bind("<<ListboxSelect>>", self.t_pick)

        left.pack(side="left", fill="both", expand=True)
        right.pack(side="left", fill="both", expand=True)
        self.refresh()

        if prefill_pattern:
            self.t_pat.insert(0, prefill_pattern)
            if sample_line:
                self.t_test.insert(0, sample_line)
            self.t_check()
            self.t_cmd.focus_set()

    # ------------------------------------------------------ shared
    def refresh(self):
        c = self.c
        self.a_lb.delete(0, "end")
        self.a_names = []
        for k in sorted(c.aliases):
            scope = c.cascade.source_of("aliases", k) or "?"
            self.a_lb.insert("end", f"({scope}) {k} = {c.aliases[k]}")
            self.a_names.append(k)
        self.t_lb.delete(0, "end")
        self.t_patterns = []
        for pat, _x, body, snd in c.triggers:
            scope = c.cascade.source_of("triggers", pat) or "?"
            mark = " *snd" if snd else ""
            if c.trigger_modes.get(pat):
                mark += f" [{c.trigger_modes[pat]}]"
            self.t_lb.insert("end", f"({scope}) /{pat}/ = {body}{mark}")
            self.t_patterns.append(pat)

    # ------------------------------------------------------ aliases
    def a_pick(self, _e=None):
        sel = self.a_lb.curselection()
        if not sel:
            return
        name = self.a_names[sel[0]]
        self.a_name.delete(0, "end"); self.a_name.insert(0, name)
        self.a_body.delete(0, "end")
        self.a_body.insert(0, self.c.aliases.get(name, ""))
        scope = self.c.cascade.source_of("aliases", name)
        if scope:
            self.a_scope.var.set(scope)

    def a_save(self):
        name, body = self.a_name.get().strip(), self.a_body.get().strip()
        if not name or not body:
            return
        err = self.c.cascade.save_entry(self.a_scope.scope, "aliases",
                                        name, body)
        if err:
            self.c.write_local(f"save failed: {err}", "#cc6666")
        self.c.load_layers()
        self.refresh()

    def a_delete(self):
        name = self.a_name.get().strip()
        scope = self.c.cascade.source_of("aliases", name)
        if scope is None:
            return
        err = self.c.cascade.delete_entry(scope, "aliases", name)
        if err:
            self.c.write_local(f"delete failed: {err}", "#cc6666")
        self.c.load_layers()
        self.refresh()

    def a_move(self):
        name = self.a_name.get().strip()
        src = self.c.cascade.source_of("aliases", name)
        dst = self.a_scope.scope
        if src is None or src == dst:
            return
        err = self.c.cascade.move_entry(src, dst, "aliases", name)
        if err:
            self.c.write_local(f"move failed: {err}", "#cc6666")
        else:
            self.c.write_local(
                f"[alias '{name}' moved {src} -> "
                f"{self.c.cascade.label_for(dst)}]", "#66cc66")
        self.c.load_layers()
        self.refresh()

    # ------------------------------------------------------ triggers
    def browse_snd(self):
        p = filedialog.askopenfilename(
            parent=self, title="Choose .wav",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")])
        if p:
            self.t_snd.delete(0, "end"); self.t_snd.insert(0, p)

    def t_check(self, *_a):
        pat, sample = self.t_pat.get(), self.t_test.get()
        if not pat:
            self.t_result.configure(text="", fg="#8899aa")
            return
        try:
            rx = re.compile(pat)
        except re.error as e:
            self.t_result.configure(text=f"bad regex: {e}",
                                    fg="#ff6666")
            return
        if not sample:
            self.t_result.configure(text="regex OK", fg="#66cc66")
            return
        mt = rx.search(sample)
        if mt:
            groups = "  ".join(f"\\{i+1}='{g}'" for i, g in
                               enumerate(mt.groups()))
            self.t_result.configure(
                text="MATCH" + (f"   {groups}" if groups else ""),
                fg="#66cc66")
        else:
            self.t_result.configure(text="no match", fg="#cc9933")

    def t_pick(self, _e=None):
        sel = self.t_lb.curselection()
        if not sel:
            return
        pat = self.t_patterns[sel[0]]
        item = self.c.trigger_item(pat) or {}
        self.t_pat.delete(0, "end"); self.t_pat.insert(0, pat)
        self.t_cmd.delete(0, "end")
        self.t_cmd.insert(0, item.get("command", ""))
        self.t_snd.delete(0, "end")
        self.t_snd.insert(0, item.get("sound", ""))
        scope = self.c.cascade.source_of("triggers", pat)
        if scope:
            self.t_scope.var.set(scope)
        self.t_check()

    def t_save(self):
        pat, body = self.t_pat.get().strip(), self.t_cmd.get().strip()
        snd = self.t_snd.get().strip()
        if not pat or not (body or snd):
            return
        try:
            re.compile(pat)
        except re.error:
            self.t_check()
            return
        item = self.c.trigger_item(pat) or {"pattern": pat}
        item["command"] = body
        if snd:
            item["sound"] = snd
        else:
            item.pop("sound", None)
        err = self.c.cascade.save_entry(self.t_scope.scope, "triggers",
                                        None, item)
        if err:
            self.c.write_local(f"save failed: {err}", "#cc6666")
        self.c.load_layers()
        self.refresh()

    def t_delete(self):
        pat = self.t_pat.get().strip()
        scope = self.c.cascade.source_of("triggers", pat)
        if scope is None:
            return
        err = self.c.cascade.delete_entry(scope, "triggers", pat)
        if err:
            self.c.write_local(f"delete failed: {err}", "#cc6666")
        self.c.load_layers()
        self.refresh()

    def t_move(self):
        pat = self.t_pat.get().strip()
        src = self.c.cascade.source_of("triggers", pat)
        dst = self.t_scope.scope
        if src is None or src == dst:
            return
        err = self.c.cascade.move_entry(src, dst, "triggers", pat)
        if err:
            self.c.write_local(f"move failed: {err}", "#cc6666")
        else:
            self.c.write_local(
                f"[trigger /{pat}/ moved {src} -> "
                f"{self.c.cascade.label_for(dst)}]", "#66cc66")
        self.c.load_layers()
        self.refresh()


class KeybindDialog(tk.Toplevel):
    """Keybindings editor (spec 4.1): press the key to capture its
    keysym (the user never needs to know 'KP_Divide'), type the
    command, pick the scope, save. Writes through the same
    read-modify-write path as the builder; hand-editing the json
    stays equally valid. NumLock twin keysyms are bound together."""

    NUMLOCK_TWINS = {
        "KP_0": "KP_Insert", "KP_Insert": "KP_0",
        "KP_1": "KP_End", "KP_End": "KP_1",
        "KP_2": "KP_Down", "KP_Down": "KP_2",
        "KP_3": "KP_Next", "KP_Next": "KP_3",
        "KP_4": "KP_Left", "KP_Left": "KP_4",
        "KP_5": "KP_Begin", "KP_Begin": "KP_5",
        "KP_6": "KP_Right", "KP_Right": "KP_6",
        "KP_7": "KP_Home", "KP_Home": "KP_7",
        "KP_8": "KP_Up", "KP_Up": "KP_8",
        "KP_9": "KP_Prior", "KP_Prior": "KP_9",
        "KP_Decimal": "KP_Delete", "KP_Delete": "KP_Decimal",
    }

    def __init__(self, client):
        super().__init__(client.root)
        self.c = client
        self.title("Keybindings")
        self.configure(bg=BG)
        self.geometry("520x480")
        self.transient(client.root)

        tk.Label(self, text="Bindings (scope shown per entry):",
                 bg=BG, fg=FG).pack(anchor="w", padx=10, pady=(10, 2))
        self.lb = tk.Listbox(self, bg=FIELD_BG, fg=FG,
                             selectbackground=SEL_BG,
                             exportselection=False)
        self.lb.pack(fill="both", expand=True, padx=10)
        self.lb.bind("<<ListboxSelect>>", self.pick)
        self.keysyms = []

        cap = tk.Frame(self, bg=BG)
        tk.Label(cap, text="Key:", bg=BG, fg=FG).pack(side="left")
        self.key_var = tk.StringVar(value="(press Capture)")
        tk.Label(cap, textvariable=self.key_var, bg=FIELD_BG, fg=FG,
                 width=14).pack(side="left", padx=6)
        self.cap_btn = tk.Button(cap, text="Capture",
                                 command=self.capture, bg=SEL_BG,
                                 fg="#ffffff")
        self.cap_btn.pack(side="left", padx=4)
        cap.pack(anchor="w", padx=10, pady=(8, 2))

        tk.Label(self, text="Command:", bg=BG, fg=FG).pack(
            anchor="w", padx=10)
        self.cmd = tk.Entry(self, bg=ENTRY_BG, fg="#eeeeee",
                            insertbackground="#eeeeee")
        self.cmd.pack(fill="x", padx=10)

        self.scope = ScopeBar(self, client)
        self.scope.pack(anchor="w", padx=10, pady=6)

        btns = tk.Frame(self, bg=BG)
        tk.Button(btns, text="Save", command=self.save, bg=SEL_BG,
                  fg="#ffffff", width=8).pack(side="left", padx=4)
        tk.Button(btns, text="Delete", command=self.delete,
                  bg="#663333", fg="#ffffff", width=8).pack(
                      side="left", padx=4)
        btns.pack(pady=8)
        self._capturing = False
        self.refresh()

    def refresh(self):
        self.lb.delete(0, "end")
        self.keysyms = []
        for k in sorted(self.c.keys):
            scope = self.c.cascade.source_of("keys", k) or "?"
            self.lb.insert("end", f"({scope}) {k:>12} -> "
                                  f"{self.c.keys[k]}")
            self.keysyms.append(k)

    def pick(self, _e=None):
        sel = self.lb.curselection()
        if not sel:
            return
        k = self.keysyms[sel[0]]
        self.key_var.set(k)
        self.cmd.delete(0, "end")
        self.cmd.insert(0, self.c.keys.get(k, ""))
        scope = self.c.cascade.source_of("keys", k)
        if scope:
            self.scope.var.set(scope)

    def capture(self):
        if self._capturing:
            return
        self._capturing = True
        self.cap_btn.configure(text="press a key...", bg="#665533")
        self.bind("<KeyPress>", self._captured)
        self.focus_set()

    def _captured(self, event):
        # ignore bare modifiers
        if event.keysym in ("Shift_L", "Shift_R", "Control_L",
                            "Control_R", "Alt_L", "Alt_R"):
            return "break"
        keysym = event.keysym
        if sys_win_keycode := getattr(event, "keycode", None):
            from .client import WIN_NUMPAD_KEYCODES
            import sys as _sys
            if _sys.platform == "win32" and \
                    sys_win_keycode in WIN_NUMPAD_KEYCODES:
                keysym = WIN_NUMPAD_KEYCODES[sys_win_keycode]
        self.key_var.set(keysym)
        self.unbind("<KeyPress>")
        self._capturing = False
        self.cap_btn.configure(text="Capture", bg=SEL_BG)
        return "break"

    def save(self):
        k = self.key_var.get()
        cmd = self.cmd.get().strip()
        if not k or k.startswith("(") or not cmd:
            return
        scope = self.scope.scope
        err = self.c.cascade.save_entry(scope, "keys", k, cmd)
        twin = self.NUMLOCK_TWINS.get(k)
        if twin and not err:
            err = self.c.cascade.save_entry(scope, "keys", twin, cmd)
        if err:
            self.c.write_local(f"save failed: {err}", "#cc6666")
        self.c.load_layers()
        self.refresh()

    def delete(self):
        k = self.key_var.get()
        scope = self.c.cascade.source_of("keys", k)
        if scope is None:
            return
        self.c.cascade.delete_entry(scope, "keys", k)
        twin = self.NUMLOCK_TWINS.get(k)
        if twin and self.c.cascade.source_of("keys", twin) == scope:
            self.c.cascade.delete_entry(scope, "keys", twin)
        self.c.load_layers()
        self.refresh()


class FontDialog(tk.Toplevel):
    def __init__(self, client):
        super().__init__(client.root)
        self.c = client
        s = client.profiles_data.setdefault("settings", {})
        self.title("Font")
        self.configure(bg=BG)
        self.geometry("380x440")
        self.transient(client.root)

        tk.Label(self, text="Typeface:", bg=BG, fg=FG).pack(
            anchor="w", padx=10, pady=(10, 2))
        frame = tk.Frame(self)
        self.lb = tk.Listbox(frame, bg=FIELD_BG, fg=FG,
                             selectbackground=SEL_BG,
                             exportselection=False)
        sb = tk.Scrollbar(frame, command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        preferred = ["Consolas", "Cascadia Mono", "Cascadia Code",
                     "Lucida Console", "Courier New", "Fixedsys"]
        all_fams = sorted({f for f in tkfont.families()
                           if not f.startswith("@")})

        def is_mono(fam):
            try:
                return bool(tkfont.Font(family=fam,
                                        size=10).metrics("fixed"))
            except tk.TclError:
                return False

        if not hasattr(client, "_mono_cache"):
            client._mono_cache = {f: is_mono(f) for f in all_fams}

        self.mono_only = tk.BooleanVar(value=True)
        self.ordered = []
        self.cur_fam = s.get("font_family",
                             client.base_font.cget("family"))

        def fill_list():
            self.ordered.clear()
            self.lb.delete(0, "end")
            fams = [f for f in all_fams
                    if client._mono_cache.get(f)] \
                if self.mono_only.get() else all_fams
            self.ordered.extend(
                [f for f in preferred if f in fams] +
                [f for f in fams if f not in preferred])
            for f in self.ordered:
                self.lb.insert("end", f)
            if self.cur_fam in self.ordered:
                idx = self.ordered.index(self.cur_fam)
                self.lb.selection_set(idx)
                self.lb.see(idx)
            elif self.ordered:
                self.lb.selection_set(0)

        fill_list()
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        frame.pack(fill="both", expand=True, padx=10)
        tk.Checkbutton(self, text="Monospaced fonts only",
                       variable=self.mono_only, command=fill_list,
                       bg=BG, fg=FG, activebackground=BG,
                       activeforeground=FG,
                       selectcolor=FIELD_BG).pack(anchor="w", padx=10)

        tk.Label(self, text="Size:", bg=BG, fg=FG).pack(
            anchor="w", padx=10, pady=(8, 2))
        self.size_var = tk.IntVar(
            value=s.get("font_size", client.base_font.cget("size")))
        tk.Spinbox(self, from_=7, to=32, textvariable=self.size_var,
                   width=5, bg=FIELD_BG, fg=FG,
                   insertbackground=FG).pack(anchor="w", padx=10)

        self.sample_font = tkfont.Font(family=self.cur_fam,
                                       size=self.size_var.get())
        tk.Label(self, text="You attack the troll. 1234 [n e sw]",
                 bg="#101014", fg="#cccccc", font=self.sample_font,
                 pady=8).pack(fill="x", padx=10, pady=8)

        self.lb.bind("<<ListboxSelect>>", self.preview)
        self.size_var.trace_add("write", lambda *_a: self.preview())

        btns = tk.Frame(self, bg=BG)
        tk.Button(btns, text="Apply", command=self.apply_close,
                  bg=SEL_BG, fg="#ffffff", width=10).pack(side="left",
                                                          padx=6)
        tk.Button(btns, text="Cancel", command=self.destroy,
                  bg="#333333", fg=FG, width=10).pack(side="left",
                                                      padx=6)
        btns.pack(pady=8)
        self.grab_set()

    def preview(self, *_a):
        sel = self.lb.curselection()
        fam = self.ordered[sel[0]] if sel else self.cur_fam
        try:
            self.sample_font.configure(family=fam,
                                       size=self.size_var.get())
        except tk.TclError:
            pass

    def apply_close(self):
        sel = self.lb.curselection()
        fam = self.ordered[sel[0]] if sel else self.cur_fam
        self.c.apply_fonts(family=fam, size=self.size_var.get())
        self.destroy()
