"""3s Elemental guild helpers.

Small by design: the guild's numbers already arrive on feeds the client
handles generically. FFF carries Energy (gp1) and Consistency (gp2),
GMCP's Guild.Extra carries the ability counters, and Char.Vitals states
the real hp/sp maxima. What is NOT on any structured feed is elemental
negation, which appears only as a line of plain prompt text - hence this
module.
"""
import re
import tkinter as tk

from . import widgets

# Guild.Extra fields, for whenever a readout gets built. Two of these are
# in NEITHER docs/gmcp/elementals.txt NOR the 2026-08-29 capture - they
# appeared on 2026-09-03 (logs/gmcp_20260903_154720.log), so the guild is
# under active development and the doc drifts behind it.
#
# GAME FACTs (user, 2026-09-03):
#   natural_convergence  charges of a limited power that FULLY RESTORES
#                        sp. Two of them; the refresh period is uncertain
#                        (week/day/reset) - do NOT put a timer on it
#                        without checking.
#   equipollent          a toggle ("Off"/...): when on, attacks hit
#                        EVERY target in the room instead of one, for
#                        considerably less damage each.
#
# Also on that feed: consistency (95-100 live, INTEGER - no precision
# gain over FFF's gp2, which is why the bar stays on FFF), waves/
# engulfs/shrouds/blasts/emit, eshield_remaining, panic_action +
# panic_hp, resolve_type + resolve_efficiency, prismatic_emission.
#
# Beware: half that package's traffic is a mud-side debug stub,
# `Guild.Extra { "full": 1, "guild": "elemental", "test": "test" }`,
# 94 of them against 94 real packets. Harmless, but it is not data.

# The ten damage types elemental negation covers, in the order the mud
# prints them. GAME FACT (user, 2026-09-03): negation is the endgame
# defensive ability and this line should ALWAYS list all ten - a shorter
# list means negation has lapsed against the missing types.
#
# Confirmed identical in every capture: 129 occurrences in
# logs/marten-20260903.log and 70 in logs/gmcp_20260829_163123.log, all
# byte-for-byte 'NEG:SL CR FI IC AC SH ME MA TO RA'.
NEGATION_TYPES = ("SL", "CR", "FI", "IC", "AC", "SH", "ME", "MA", "TO",
                  "RA")

NEGATION_NAMES = {
    "SL": "slash", "CR": "crush", "FI": "fire", "IC": "ice",
    "AC": "acid", "SH": "shock", "ME": "mental", "MA": "magic",
    "TO": "toxic", "RA": "radiation",
}

_NEG_RE = re.compile(r"\bNEG:((?:\s*[A-Z]{2})*)\s*$")


def parse_negation(line):
    """A prompt line -> the tuple of damage types negation is active
    against, or None if this is not a negation line.

    An EMPTY tuple is a real answer ('NEG:' with nothing after it, i.e.
    negation is down entirely) and must not be confused with None.

    Only the ten known type codes are returned, so an unrecognised code
    cannot silently pass as coverage; it is dropped and therefore reads
    as missing, which errs toward warning."""
    m = _NEG_RE.search(line or "")
    if not m:
        return None
    seen = m.group(1).split()
    return tuple(t for t in NEGATION_TYPES if t in seen)


def negation_gaps(active):
    """The types negation is NOT covering, in the mud's own order."""
    have = set(active or ())
    return tuple(t for t in NEGATION_TYPES if t not in have)


def describe_gaps(gaps):
    """'FI (fire), RA (radiation)' for a warning line."""
    return ", ".join("%s (%s)" % (t, NEGATION_NAMES.get(t, "?"))
                     for t in gaps)


# Guild.Extra bookkeeping that is not guild data.
EXTRA_SKIP = ("full", "guild", "test")


def extra_lines(extra):
    """Guild.Extra state -> info-pane lines.

    Every field is optional: the feed is a delta after its first `full`
    snapshot, so a partial state is normal and a missing field is simply
    left out rather than rendered as a blank or a None.

    `consistency` is deliberately absent - it has its own vitals bar, and
    repeating it here would spend a pane line on something already on
    screen."""
    if not isinstance(extra, dict) or not extra:
        return []
    out = []

    def g(*names):
        return [extra[n] for n in names if extra.get(n) is not None]

    # what your damage is doing
    bits = []
    if extra.get("resolve_type"):
        eff = extra.get("resolve_efficiency")
        bits.append("resolve %s%s" % (extra["resolve_type"],
                                      " %s%%" % eff if eff is not None
                                      else ""))
    if extra.get("prismatic_emission"):
        bits.append("prism %s" % extra["prismatic_emission"])
    if bits:
        out.append("  ".join(bits))

    # ability charges, the numbers you actually spend
    counts = [("W", "waves"), ("E", "engulfs"), ("S", "shrouds"),
              ("B", "blasts"), ("emit ", "emit")]
    made = ["%s%s" % (sym, extra[key])
            for sym, key in counts if extra.get(key) is not None]
    if extra.get("eshield_remaining") is not None:
        made.append("eshield %s" % extra["eshield_remaining"])
    if made:
        out.append(" ".join(made))

    # GAME FACT (user, 2026-09-03): natural convergence fully restores
    # sp and you get two of them, refresh period uncertain. Equipollent
    # is a toggle that spreads every attack across the whole room for
    # much less damage each - it changes what every swing does, so it is
    # called out by name rather than hidden in a symbol.
    tail = []
    if extra.get("natural_convergence") is not None:
        tail.append("convergence %s" % extra["natural_convergence"])
    if extra.get("equipollent") is not None:
        tail.append("equipollent %s" % extra["equipollent"])
    if tail:
        out.append("  ".join(tail))

    if extra.get("panic_action"):
        hp = extra.get("panic_hp")
        out.append("panic: %s%s" % (extra["panic_action"],
                                    " @ %s" % hp if hp is not None else ""))
    return out


def info_lines(info):
    """Guild.Info -> the one figure worth a pane line: link energy.

    GAME FACT (user, 2026-09-03): link energy reaching ZERO KILLS you -
    a second death condition, entirely separate from consistency - and
    `dissipate` and `siphon` both drain it every round. It builds as you
    play and only a few powers spend it, so the headroom is usually
    enormous (146 million when this was written); the point is that
    nothing watched it at all before.

    The package's other four figures (used/to_spend/lifetime energy and
    reset) are guild-currency bookkeeping, not survival, so they are left
    out - the lethal one is what earns the line. Thousands separators
    because a nine-digit number is unreadable without them."""
    if not isinstance(info, dict):
        return []
    link = info.get("link_energy")
    if not isinstance(link, (int, float)) or isinstance(link, bool):
        return []
    return ["link: {:,}".format(int(link))]


# The defensive states on Guild.State.defs - a THIRD Guild.State shape,
# alongside the paginated skills sweep and the Viking-style fx carrier.
# In neither docs/gmcp/elementals.txt nor any capture before
# logs/gmcp_20260903_160756.log. A state is ACTIVE when its value is
# non-empty; the value itself is a prompt abbreviation ("WV"), too
# cryptic to show, so the state is named instead.
#
# GAME FACTs (user, 2026-09-03), which is the only reason these can be
# labelled at all - nothing on the wire explains them:
#
#   dissipated  mist form, invisible, but DRAINS LINK ENERGY every round.
#               Link energy at zero KILLS you - a second death condition
#               entirely separate from consistency. Guild.Info.link_energy
#               carries it (146M at the time of writing, so the danger is
#               a long way off, but the mechanism is real).
#   siphon      toggle: slowly gain energy, ALSO draining link energy.
#   diffused    toggle: drastically lowers damage dealt AND taken, drops
#               consistency to 50% and CAPS it at 50% while held. So an
#               amber Consistency bar is EXPECTED while diffused - the
#               note exists so that does not read as a fault.
#   barrier     toggle: small defensive boost, slightly less damage out.
#   wave        limited (~5-6 a reset): builds for a few rounds, then
#               feeds sp and RAISES consistency until it times out.
#   engulfed    13 armours a day; boosts an armour's ac aligned to the
#               damage type you are evoking.
#   eshield_rounds  elemental shield, twice a week: complete immunity to
#               one chosen damage type, limited duration. Rounds left.
DEFS_LABELS = (
    ("dissipated", "dissipate (drains link)"),
    ("siphon", "siphon (drains link)"),
    ("diffused", "diffuse (consistency capped 50%)"),
    ("barrier", "barrier"),
    ("wave", "wave"),
    ("engulfed", "engulf"),
)


def defs_lines(defs):
    """Guild.State.defs -> info-pane lines naming the ACTIVE defences.

    Only active states are listed: a pane that always shows all seven,
    six of them off, is a pane nobody reads. `negation` is deliberately
    skipped - it has its own monitor, which warns on change rather than
    occupying a line permanently."""
    if not isinstance(defs, dict) or not defs:
        return []
    on = [label for key, label in DEFS_LABELS if str(defs.get(key) or "")]
    rounds = defs.get("eshield_rounds")
    if isinstance(rounds, (int, float)) and rounds > 0:
        on.append("eshield %sr" % rounds)
    return ["defs: " + "  ".join(on)] if on else []


# --- `guild score` / `skills` text scraping ---------------------------
# The ONLY complete source for the skill table. GMCP's Guild.State sweep
# truncates at 32 of 48 fields (see docs/gmcp/port.md), so eleven skills
# have no maximum there - and Cohesion, below, is not in the GMCP field
# list at all. The score box and the `skills` readout print the same
# 'Name : cur/max(cost)' rows, so one parser serves both.
_SKILL_RE = re.compile(r"([A-Za-z]+)\s*:\s*(\d+)/(\d+)\(([-\d]+)\)")
_FORM_RE = re.compile(r"Form\s*:\s*(\w+)")
_SIZE_RE = re.compile(r"Elemental Size\s*:\s*(\d+)/(\d+)")
_PLATEAU_RE = re.compile(r"Next Plateau\s*:\s*(\d+)%")
_NEXTSIZE_RE = re.compile(r"Next \w+ Size\s*:\s*(\d+)")
_ENERGY_RE = re.compile(r"(Used|Link|Lifetime) Energy\s*:\s*(\d+)")
_SPEND_RE = re.compile(r"Energy (?:to Spend|available to spend)\s*:\s*(\d+)",
                       re.I)


# The prompt's own G2N line, e.g. "G2N: 285387 | B:4 W:6 R:9%". It prints
# with every prompt and is already captured (client.stat2) for the status
# bar under the hpbars - it was just never parsed.
#
# GAME FACT (user, 2026-09-04): for an ELEMENTAL this G2N is the SIZE track,
# not the plateau one - 285,387 to the next size against 13,931,835 to the
# next plateau. So it is the live source for `Next Time Size`, which the
# `guild score` readout otherwise only reveals when the player types it.
#
# Do NOT confuse it with the MIP FFF J field's "G2N:", which carried the
# multi-million PLATEAU figure - same label, different quantity. This parses
# the TEXT line only.
_G2N_SIZE_RE = re.compile(r"^G2N:\s*([\d,]+)")


def parse_g2n_size(line):
    """Prompt G2N line -> GXP to the next elemental size, or None."""
    m = _G2N_SIZE_RE.match((line or "").strip())
    return int(m.group(1).replace(",", "")) if m else None


def scrape_score(state, line):
    """Accumulate one line of `guild score` / `skills` into `state`.

    Returns True if the line contributed anything. Both readouts are
    plain text, so this is called for every line and must be cheap and
    inert on the ones that do not match."""
    if not line:
        return False
    hit = False
    for name, cur, mx, cost in _SKILL_RE.findall(line):
        # '(-)' is maxed. None rather than 0: a zero cost would sort to
        # the top of a "what can I afford" list, which is backwards.
        state.setdefault("skills", {})[name] = (
            int(cur), int(mx), None if cost == "-" else int(cost))
        hit = True
    m = _FORM_RE.search(line)
    if m:
        state["form"] = m.group(1); hit = True
    m = _SIZE_RE.search(line)
    if m:
        # GAME FACT (user, 2026-09-03): size is the guild level (fast)
        # and plateau the slow track beside it.
        state["size"], state["plateau"] = int(m.group(1)), int(m.group(2))
        hit = True
    m = _PLATEAU_RE.search(line)
    if m:
        state["next_plateau_pct"] = int(m.group(1)); hit = True
    m = _NEXTSIZE_RE.search(line)
    if m:
        state["next_size"] = int(m.group(1)); hit = True
    for which, amount in _ENERGY_RE.findall(line):
        state[which.lower() + "_energy"] = int(amount); hit = True
    m = _SPEND_RE.search(line)
    if m:
        state["energy_to_spend"] = int(m.group(1)); hit = True
    return hit


def next_emit_plateau(plateau):
    """The next plateau that raises max emit, or None.

    GAME FACT (user, 2026-09-03): max emit - the energy spent per attack
    and the damage it does - goes up at every plateau ENDING IN 6. Sizes
    come fast, plateaus are a long haul, so the next bump is worth
    naming.

    The gap is reported in PLATEAUS, never projected into guild levels.
    The user's own two intervals were 56 glvls (17->18) and 60 (18->19),
    i.e. rising - so extrapolating from two samples would understate the
    distance, and a confidently wrong estimate is worse than the honest
    count of plateaus remaining."""
    if isinstance(plateau, bool) or not isinstance(plateau, int) \
            or plateau < 0:
        return None
    nxt = (plateau // 10) * 10 + 6
    return nxt if nxt > plateau else nxt + 10


# --- the detached status panel ----------------------------------------
BG = "#0c0c12"


class ElementalStatus(tk.Toplevel):
    """Detached 'Elemental Status' panel.

    Deliberately ONE pane, not the Viking window's twelve tabs: that guild
    has a settlement, a fleet, a trade network and a map to separate, and
    this one is a screen of numbers. Tabs can come later if it earns them.

    Fed from three places, each because it is the best source for its own
    fields - see docs/gmcp/port.md:
      * GMCP Guild.Info   link/lifetime/spend energy, live all session
      * GMCP Guild.Extra  ability charges and the damage-type settings
      * `guild score` /   the SKILL table, because GMCP's Guild.State
        `skills` text     sweep truncates at 32 of 48 fields and omits
                          Cohesion entirely

    update_state() is idempotent; the client may call it on every packet.
    """

    def __init__(self, master, fonts=None, on_close=None, geometry=None,
                 topmost=False):
        super().__init__(master)
        self.title("Elemental Status")
        self.configure(bg=BG)
        self.geometry(geometry or "520x560")
        self.on_close = on_close
        self.protocol("WM_DELETE_WINDOW", self._closed)
        widgets.add_window_menu(self, topmost)
        f = fonts or {}
        self.mono = f.get("mono", ("Consolas", 11))
        sb = tk.Scrollbar(self)
        sb.pack(side="right", fill="y")
        self.txt = tk.Text(self, bg=BG, fg="#cccccc", font=self.mono,
                           wrap="none", state="disabled", padx=10, pady=8,
                           highlightthickness=0, borderwidth=0,
                           yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.configure(command=self.txt.yview)
        for tag, colour in (("sec", "#d79030"), ("val", "#cccccc"),
                            ("dim", "#777777"), ("num", "#dddddd"),
                            ("cyan", "#6fb6d6"), ("warn", "#d65151"),
                            ("ok", "#46c246")):
            self.txt.tag_configure(tag, foreground=colour)
        self.txt.tag_configure("sec", foreground="#d79030",
                               font=f.get("mono_bold",
                                          ("Consolas", 11, "bold")))

    def _closed(self):
        if self.on_close:
            self.on_close()
        self.destroy()

    def _row(self, left, right, tag="num"):
        pad = 34 - len(left) - len(right)
        self.txt.insert("end", "  " + left, "val")
        self.txt.insert("end", " " * max(1, pad), "dim")
        self.txt.insert("end", right + "\n", tag)

    def update_state(self, score, info, extra, defs):
        """Redraw from the four state dicts the client keeps."""
        if not self.winfo_exists():
            return
        pos = self.txt.yview()[0]
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        score = score or {}
        info = info or {}

        form = score.get("form")
        if form or score.get("size") is not None:
            self.txt.insert("end", "%s Elemental\n" % (form or "?"), "sec")
            if score.get("size") is not None:
                self._row("size (guild level)", str(score["size"]))
            plateau = score.get("plateau")
            if plateau is not None:
                self._row("plateau", str(plateau))
                nxt = next_emit_plateau(plateau)
                if nxt is not None:
                    # GAME FACT: max emit rises at every plateau ending
                    # in 6, and plateaus are the slow track - so the gap
                    # to the next one is the number worth seeing.
                    self._row("next emit bump", "plateau %d (+%d)"
                              % (nxt, nxt - plateau), "cyan")
            if score.get("next_plateau_pct") is not None:
                self._row("next plateau", "%d%%"
                          % score["next_plateau_pct"], "cyan")
            if score.get("next_size") is not None:
                self._row("next size", "{:,}".format(score["next_size"]))
            self.txt.insert("end", "\n")

        # Energy: GMCP first (it updates all session), score box as the
        # fallback for a MIP-only run.
        self.txt.insert("end", "Energy\n", "sec")
        got = False
        for label, key, tag in (("link (lethal at 0)", "link_energy", "warn"),
                                ("lifetime", "lifetime_energy", "num"),
                                ("to spend", "energy_to_spend", "ok"),
                                ("used", "used_energy", "dim")):
            val = info.get(key, score.get(key))
            if isinstance(val, (int, float)):
                self._row(label, "{:,}".format(int(val)), tag)
                got = True
        if not got:
            self.txt.insert("end", "  (no energy data yet)\n", "dim")
        self.txt.insert("end", "\n")

        for line in extra_lines(extra) + defs_lines(defs):
            self.txt.insert("end", "  " + line + "\n", "val")
        if extra or defs:
            self.txt.insert("end", "\n")

        skills = (score or {}).get("skills") or {}
        self.txt.insert("end", "Skills\n", "sec")
        if not skills:
            self.txt.insert("end",
                            "  none yet - type `guild score` or `skills`\n",
                            "dim")
        else:
            # Un-maxed first, cheapest next: the only rows you can act on
            # are the ones with a cost, and a maxed skill is just history.
            def order(item):
                _name, (_cur, _mx, cost) = item
                return (cost is None, cost if cost is not None else 0)
            # Fixed columns: costs run from five digits to seven, so a
            # single right-aligned string leaves the numbers stepped and
            # unscannable - which is the one thing this table is for.
            for name, (cur, mx, cost) in sorted(skills.items(), key=order):
                self.txt.insert("end", "  " + name.ljust(15), "val")
                self.txt.insert("end", ("%d/%d" % (cur, mx)).rjust(9),
                                "num")
                if cost is None:
                    self.txt.insert("end", "      maxed\n", "dim")
                else:
                    self.txt.insert("end",
                                    "{:,}".format(cost).rjust(11) + "\n",
                                    "cyan")
        self.txt.configure(state="disabled")
        self.txt.yview_moveto(pos)
