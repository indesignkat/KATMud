"""katmud_lib.angel - 3s Angel text decoders.

Angels have no GMCP source for Essence or Possession Health (the angel
Guild.State/Info packets carry only spec, cons_cooldown, transfigurations
and gxp), so after MIP's FFF feed went away mud-side on 2026-09-15 the
default hpbar prompt is the only place either pool appears:

    H: 1491/1514[-23]|2232/2255[-23] E: 701/710[-9](36) G: 30%

`|cur/max` after HP is Possession Health (0/0 when corporeal), `E:` on
THIS line is Essence. The prompt's second line also has an `E:` (Enemy),
which is why the match is anchored on the `H:` line's own shape. An angel
with a custom `hpbar` gets no bars.
"""
import re

_PROMPT_RE = re.compile(
    r"^H: \d+/\d+\[-?\d+\]\|(\d+)/(\d+)\[-?\d+\] E: (\d+)/(\d+)\[")


def parse_prompt(line):
    """Prompt H: line -> {gp1, gp1max, gp2, gp2max}, or None."""
    m = _PROMPT_RE.match(line or "")
    if not m:
        return None
    php, phpmax, ess, essmax = map(int, m.groups())
    return {"gp1": ess, "gp1max": essmax, "gp2": php, "gp2max": phpmax}


# `gh transfigurations` (in-game, logs/killtimer-20261002.log), as
# (glvl, name). Wings+ (divine, glvl 75) is left out on purpose: it is
# the same Wings, just better from 75 on - not a separate transfig.
DIVINE = ((1, "Halo"), (5, "Wings"), (15, "Radiance"), (25, "Retribution"),
          (35, "Good"), (40, "Prowess"), (50, "Perseverance"), (65, "Aegis"),
          (100, "Aether"))
FALLEN = ((1, "Shadowflame"), (5, "Claws"), (15, "Darkfire"),
          (25, "Bloodrage"), (35, "Evil"), (40, "Prowess"),
          (50, "Perseverance"), (65, "Aegis"), (75, "Tenebra"),
          (100, "Aether"))
_ONLY = {"divine": {n for _, n in DIVINE} - {n for _, n in FALLEN},
         "fallen": {n for _, n in FALLEN} - {n for _, n in DIVINE}}

_SCORE_GLVL_RE = re.compile(r"\bGuild\s*:\s*Angel \((\d+)\)")


def parse_transfigs(text):
    """GMCP Guild.State `transfigurations` -> set of shown names."""
    names = {n.strip() for n in (text or "").split(",")}
    return names - {"", "None"}


def side_of(names):
    """'divine' / 'fallen' from any one-side-only name, else None."""
    for side, only in _ONLY.items():
        if names & only:
            return side
    return None


def available(side, glvl):
    """The side's transfigs up to glvl. Unknown glvl stops short of 100."""
    table = DIVINE if side == "divine" else FALLEN
    top = glvl if glvl is not None else 99
    return [n for lvl, n in table if lvl <= top]


def parse_score_glvl(line):
    """`sc` line `Guild    : Angel (95)` -> 95, or None."""
    m = _SCORE_GLVL_RE.search(line or "")
    return int(m.group(1)) if m else None
