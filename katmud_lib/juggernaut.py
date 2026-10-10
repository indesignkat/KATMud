"""katmud_lib.juggernaut - 3s Juggernaut decoders.

Sources (logs/gmcp_20261010_030256.log + the login readout):

  * The prompt, every combat round and on `hp`:
        Hp:[867/867] Sp:[610/482] S:[100%] H:[0%] C/M:[2/2][2/2](95%) G:73.916
    S = Stim %, H = Heat %, C/M = clan powers cur/max then missiles
    cur/max, (95%) = reset, G = % into the current suit level.
  * GMCP Guild.State  the same as stim_pct, heat_pct, clan_powers(_max),
                      missiles(_max), reset_pct, glevel_pct, plus jump_jets,
                      low_light, battery - a DELTA feed after the login
                      snapshot.
  * GMCP Guild.Info   current_suit.

GAME FACTS: Stim is how juggernauts heal themselves, so low Stim
means no healing left, not death. Battery only powers the flashlight -
not shown. Heat warning levels are a guess, not yet tuned against
play.
"""
import re

_PROMPT_RE = re.compile(
    r"\bS:\[(\d+)%\]\s*H:\[(\d+)%\]\s*C/M:\[(\d+)/(\d+)\]\[(\d+)/(\d+)\]"
    r"\((\d+)%\)(?:\s*G:([\d.]+))?")

# Guild.State field -> our key. stim/heat feed the gp1/gp2 bars.
_GMCP_INTS = {"stim_pct": "gp1", "heat_pct": "gp2", "clan_powers": "clan",
              "clan_powers_max": "clan_max", "missiles": "missiles",
              "missiles_max": "missiles_max", "reset_pct": "reset_pct",
              "jump_jets": "jump_jets", "low_light": "low_light"}


def parse_prompt(line):
    """The juggernaut prompt -> fields, or None."""
    m = _PROMPT_RE.search(line or "")
    if not m:
        return None
    out = {"gp1": int(m.group(1)), "gp2": int(m.group(2)),
           "clan": int(m.group(3)), "clan_max": int(m.group(4)),
           "missiles": int(m.group(5)), "missiles_max": int(m.group(6)),
           "reset_pct": int(m.group(7))}
    if m.group(8):
        out["glevel_pct"] = float(m.group(8))
    return out


def parse_gmcp(data):
    """Guild.State / Guild.Info -> the fields it carries."""
    out = {}
    for src, dst in _GMCP_INTS.items():
        v = data.get(src)
        if isinstance(v, int) and not isinstance(v, bool):
            out[dst] = v
    g = data.get("glevel_pct")
    if isinstance(g, (int, float)) and not isinstance(g, bool):
        out["glevel_pct"] = float(g)
    if isinstance(data.get("current_suit"), str):
        out["suit"] = data["current_suit"]
    return out


def status_line(st):
    """`Gnome 73.9%  Clan 2/2  Missiles 2/2 (95%)  Jump Jets  Low Light`;
    anything never received is left out."""
    parts = []
    if "suit" in st or "glevel_pct" in st:
        suit = st.get("suit", "").capitalize()
        pct = f"{st['glevel_pct']:.1f}%" if "glevel_pct" in st else ""
        parts.append(" ".join(p for p in (suit, pct) if p))
    if "clan" in st:
        parts.append(f"Clan {st['clan']}/{st.get('clan_max', '?')}")
    if "missiles" in st:
        reset = f" ({st['reset_pct']}%)" if "reset_pct" in st else ""
        parts.append(f"Missiles {st['missiles']}/"
                     f"{st.get('missiles_max', '?')}{reset}")
    if st.get("jump_jets"):
        parts.append("Jump Jets")
    if st.get("low_light"):
        parts.append("Low Light")
    return "  ".join(parts)
