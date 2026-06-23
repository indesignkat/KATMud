"""katmud_lib.picker - the transient launcher (spec sections 1.2/1.3).

Opens when katmud.pyw runs with no arguments. Spawns the chosen
profile's client as a DETACHED process and exits - the client is never
a window or thread of the picker, so one character crashing can never
take down another. Second connection = run the picker again.
"""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, simpledialog

from . import credentials, hub, paths, profiles

BG = "#1a1a1a"
FG = "#ccccdd"
FIELD_BG = "#101018"
SEL_BG = "#334466"


def _spawn_detached(arg):
    """Detached pythonw process running katmud.pyw <arg> - no console,
    not a child of this process (so it outlives the picker, which
    exits right after spawning). Shared by spawn_client (arg = a
    profile id) and ensure_hub_running (arg = "--hub")."""
    entry = os.path.join(paths.BASE, "katmud.pyw")
    exe = sys.executable
    if sys.platform == "win32":
        # prefer pythonw so the child has no console either
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pythonw):
            exe = pythonw
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP)
        subprocess.Popen([exe, entry, arg],
                         creationflags=flags, close_fds=True,
                         cwd=paths.BASE)
    else:
        subprocess.Popen([exe, entry, arg],
                         start_new_session=True, close_fds=True,
                         cwd=paths.BASE)


def spawn_client(profile_id):
    _spawn_detached(profile_id)


def ensure_hub_running():
    """Phone/web dashboard (spec: docs/superpowers/specs/
    2026-06-21-web-dashboard-design.md): the hub must be up for the
    dashboard to work, but it isn't tied to any one character's
    lifetime, so the picker starts it (the same detached way it starts
    characters) the first time it's not already listening."""
    host = hub.resolve_bind_host()
    if hub.is_hub_running(host, hub.DEFAULT_PORT):
        return
    _spawn_detached("--hub")


class ProfileForm(tk.Toplevel):
    """New Character / Edit form (spec 1.3).

    New: scaffolds characters/<name>.json on save (reusing an existing
    one untouched). Edit: pre-populated, includes Change Password.
    """

    def __init__(self, parent, data, entry=None, on_save=None):
        super().__init__(parent)
        self.data = data
        self.entry = entry          # None = New Character
        self.on_save = on_save
        self.title("Edit Profile" if entry else "New Character")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)

        muds = paths.list_muds() or ["3s", "3k"]

        def row(label):
            tk.Label(self, text=label, bg=BG, fg=FG, anchor="w") \
                .pack(fill="x", padx=12, pady=(8, 1))

        row("Character name (login name on the mud):")
        self.e_char = tk.Entry(self, bg=FIELD_BG, fg=FG,
                               insertbackground=FG)
        self.e_char.pack(fill="x", padx=12)

        row("Display name (for the picker / window title):")
        self.e_disp = tk.Entry(self, bg=FIELD_BG, fg=FG,
                               insertbackground=FG)
        self.e_disp.pack(fill="x", padx=12)
        self._disp_touched = False
        self.e_disp.bind("<Key>", lambda e: self._touch_disp())
        # pre-fill display from character until the user edits it
        self.e_char.bind("<KeyRelease>", self._sync_disp)

        row("Mud:")
        self.mud_var = tk.StringVar(value=muds[0])
        self.om_mud = tk.OptionMenu(self, self.mud_var, *muds,
                                    command=lambda *_a:
                                    self._refresh_guilds())
        self._style_om(self.om_mud)
        self.om_mud.pack(fill="x", padx=12)

        row("Guild:")
        self.guild_var = tk.StringVar(value="none")
        self.om_guild = tk.OptionMenu(self, self.guild_var, "none")
        self._style_om(self.om_guild)
        self.om_guild.pack(fill="x", padx=12)

        row("Port override (blank = mud default):")
        self.e_port = tk.Entry(self, bg=FIELD_BG, fg=FG,
                               insertbackground=FG)
        self.e_port.pack(fill="x", padx=12)

        btns = tk.Frame(self, bg=BG)
        tk.Button(btns, text="Save", command=self._save, bg=SEL_BG,
                  fg="#ffffff", width=10).pack(side="left", padx=6)
        if entry:
            tk.Button(btns, text="Change Password...",
                      command=self._change_password, bg="#665533",
                      fg="#ffffff").pack(side="left", padx=6)
        tk.Button(btns, text="Cancel", command=self.destroy,
                  bg="#333333", fg=FG, width=10).pack(side="left",
                                                      padx=6)
        btns.pack(pady=12)

        if entry:
            self.e_char.insert(0, entry.get("character", ""))
            self.e_char.configure(state="disabled")   # rename: out of scope
            self.e_disp.insert(0, entry.get("display_name", ""))
            self._disp_touched = True
            self.mud_var.set(entry.get("mud", muds[0]))
            self.guild_var.set(entry.get("guild", "none"))
            if entry.get("port_override"):
                self.e_port.insert(0, str(entry["port_override"]))
        self._refresh_guilds()
        self.e_char.focus_set() if not entry else self.e_disp.focus_set()
        self.grab_set()

    @staticmethod
    def _style_om(om):
        om.configure(bg=FIELD_BG, fg=FG, highlightthickness=0,
                     activebackground=SEL_BG, activeforeground="#ffffff")
        om["menu"].configure(bg=FIELD_BG, fg=FG)

    def _touch_disp(self):
        self._disp_touched = True

    def _sync_disp(self, _e=None):
        if not self._disp_touched:
            self.e_disp.delete(0, "end")
            self.e_disp.insert(0, self.e_char.get())

    def _refresh_guilds(self):
        guilds = ["none"] + paths.list_guilds(self.mud_var.get())
        menu = self.om_guild["menu"]
        menu.delete(0, "end")
        for g in guilds:
            menu.add_command(label=g,
                             command=lambda v=g: self.guild_var.set(v))
        if self.guild_var.get() not in guilds:
            # keep a hand-set guild even if its file doesn't exist yet
            # (profiles can be created before guild configs - spec 2.2)
            menu.add_command(label=self.guild_var.get(),
                             command=lambda v=self.guild_var.get():
                             self.guild_var.set(v))

    def _change_password(self):
        entry = self.entry
        pw = simpledialog.askstring(
            "Change Password",
            f"New password for {entry['character']} on "
            f"{entry['mud']}\n(stored as "
            f"{credentials.key_label(entry['mud'], entry['character'])})",
            parent=self, show="*")
        if pw is None:
            return
        err = credentials.set_password(entry["mud"],
                                       entry["character"], pw)
        if err:
            messagebox.showerror("katmud", err, parent=self)
        else:
            messagebox.showinfo("katmud", "Password stored.",
                                parent=self)

    def _save(self):
        char = self.e_char.get().strip() if not self.entry \
            else self.entry["character"]
        if not char:
            messagebox.showerror("katmud", "Character name is required.",
                                 parent=self)
            return
        disp = self.e_disp.get().strip() or char
        mud = self.mud_var.get()
        port = self.e_port.get().strip()
        port_override = None
        if port:
            try:
                port_override = int(port)
            except ValueError:
                messagebox.showerror("katmud", "Port must be a number.",
                                     parent=self)
                return
        entry = {
            "id": (self.entry["id"] if self.entry
                   else profiles.make_id(char, mud)),
            "display_name": disp,
            "character": char,
            "mud": mud,
            "guild": self.guild_var.get(),
            "config_override": (self.entry or {}).get("config_override"),
            "port_override": port_override,
            "last_launched": (self.entry or {}).get("last_launched"),
        }
        profiles.upsert(self.data, entry)
        if self.on_save:
            self.on_save()
        self.destroy()


class Picker(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KatMUD")
        self.configure(bg=BG)
        self.geometry("460x440")
        self.data, err = profiles.load()
        if err:
            messagebox.showerror("katmud", f"profiles.json: {err}")
        self.rows = []          # listbox line index -> profile id or None

        tk.Label(self, text="KatMUD - choose a character", bg=BG,
                 fg="#9999cc", font=("Segoe UI", 12, "bold")) \
            .pack(pady=(10, 4))
        self.lb = tk.Listbox(self, bg=FIELD_BG, fg=FG,
                             selectbackground=SEL_BG,
                             font=("Consolas", 11), activestyle="none")
        self.lb.pack(fill="both", expand=True, padx=12, pady=4)
        self.lb.bind("<Double-Button-1>", lambda e: self._launch())
        self.lb.bind("<Return>", lambda e: self._launch())

        btns = tk.Frame(self, bg=BG)
        tk.Button(btns, text="Launch", command=self._launch, bg=SEL_BG,
                  fg="#ffffff", width=10).pack(side="left", padx=5)
        tk.Button(btns, text="New Character", command=self._new,
                  bg="#335544", fg="#ffffff").pack(side="left", padx=5)
        tk.Button(btns, text="Edit", command=self._edit, bg="#554433",
                  fg="#ffffff", width=8).pack(side="left", padx=5)
        btns.pack(pady=10)

        self._fill()
        # First run / empty profiles: straight to the New form
        # (spec 1.3).
        self._first_run_job = None
        if not self.data["profiles"]:
            self._first_run_job = self.after(100, self._new)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _close(self):
        if self._first_run_job:
            self.after_cancel(self._first_run_job)
        self.destroy()

    # -------------------------------------------------------- list
    def _fill(self):
        self.lb.delete(0, "end")
        self.rows = []
        mru, grouped = profiles.ordered_for_picker(self.data)

        def add(text, pid=None, header=False):
            self.lb.insert("end", text)
            self.rows.append(pid)
            if header:
                self.lb.itemconfig("end", fg="#777799")

        if mru:
            add("- recent -", header=True)
            for p in mru:
                add(f"  {p.get('display_name', p['id'])}"
                    f"   [{p.get('mud','?')}/{p.get('guild','none')}]",
                    p["id"])
        for mud, plist in grouped:
            add(f"- {mud} -", header=True)
            for p in plist:
                add(f"  {p.get('display_name', p['id'])}"
                    f"   [{p.get('guild','none')}]", p["id"])
        # select first launchable row
        for i, pid in enumerate(self.rows):
            if pid:
                self.lb.selection_set(i)
                break

    def _selected_id(self):
        sel = self.lb.curselection()
        if not sel:
            return None
        return self.rows[sel[0]]

    # ----------------------------------------------------- actions
    def _launch(self):
        pid = self._selected_id()
        if not pid:
            return
        profiles.touch(self.data, pid)
        ensure_hub_running()
        spawn_client(pid)
        self.destroy()          # transient: picker exits after spawn

    def _new(self):
        ProfileForm(self, self.data, on_save=self._fill)

    def _edit(self):
        pid = self._selected_id()
        if not pid:
            return
        entry = profiles.get(self.data, pid)
        ProfileForm(self, self.data, entry=entry, on_save=self._fill)


def run():
    paths.ensure_dirs()
    Picker().mainloop()
