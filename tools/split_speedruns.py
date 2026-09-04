#!/usr/bin/env python3
"""split_speedruns.py - one-time per-mud speedruns migration (spec 6.1).

    python tools\\split_speedruns.py <shared-speedruns.tin>

Validates every .add_speedrun entry against each mud's map and writes
muds/<mud>/speedruns-<mud>.tin containing only entries whose room id
exists on that map. Entries resolving on NEITHER map are reported for
manual curation.

HONESTY NOTE (3k): the shared file was authored against the 3s map.
tt++ room ids are session-order artifacts, so an id "existing" in
3k.map does not mean it is the same place - it is whatever room got
that number in the 3k mapping session. Validation by existence would
bless ~98% wrong destinations. Therefore: 3s gets the validated file;
3k gets a header-only file by default. Pass --trust-ids to override if
you know the ids are genuinely shared.
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from katmud_lib import mapdata, paths  # noqa: E402

SPEEDRUN_RE = re.compile(
    r"\.add_speedrun\s+\{([^}]*)\}\s+\{([^}]*)\}\s+\{(\d+)\}\s+\{([^}]*)\}")

HEADER_3K = """\
#nop -- KatMUD per-mud speedruns file for 3k.
#nop -- Intentionally empty: the shared speedruns file was authored
#nop -- against the 3s map. tt++ room ids are session artifacts, so
#nop -- the same id on the 3k map is a DIFFERENT room. Add validated
#nop -- 3k entries here as: .add_speedrun {name} {type} {ttid} {desc}
"""


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    trust_ids = "--trust-ids" in sys.argv
    if not argv:
        print(__doc__)
        sys.exit(1)
    src = argv[0]
    lines = open(src, encoding="utf-8", errors="replace").readlines()

    maps = {}
    for mud in paths.list_muds():
        mp = paths.map_file(mud)
        if os.path.exists(mp):
            print(f"loading {mp} ...")
            maps[mud] = mapdata.TinMap(mp)

    entries = []
    for line in lines:
        m = SPEEDRUN_RE.search(line)
        if m:
            entries.append((m.group(1).strip(), int(m.group(3)),
                            line.rstrip("\n")))

    nowhere = []
    for name, rid, _line in entries:
        if not any(rid in tm.rooms for tm in maps.values()):
            nowhere.append((name, rid))

    for mud, tm in maps.items():
        out = paths.speedruns_file(mud)
        if mud == "3k" and not trust_ids:
            with open(out, "w", encoding="utf-8") as f:
                f.write(HEADER_3K)
            print(f"{out}: header only (see --trust-ids)")
            continue
        kept = [ln for name, rid, ln in entries if rid in tm.rooms]
        dropped = len(entries) - len(kept)
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"#nop -- KatMUD per-mud speedruns for {mud}, "
                    f"split from {os.path.basename(src)}\n")
            f.write("\n".join(kept) + "\n")
        print(f"{out}: {len(kept)} entries kept, {dropped} dropped")

    if nowhere:
        print(f"\n{len(nowhere)} entries resolve on NO map - curate "
              "manually:")
        for name, rid in nowhere:
            print(f"  {name}  (room {rid})")
    else:
        print("\nNo entries orphaned on every map.")


if __name__ == "__main__":
    main()
