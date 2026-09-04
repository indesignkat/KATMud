"""convert_tin_bots.py - import tintin '#read common/bot/generic.tin' area
bots into KatMUD bot-definition JSON.

Each source .tin is DATA fed to the shared generic.tin engine:
  #var {bot[path]} {n;e;{nod;look};pry door;...}   - the speedwalk route
  #list botmobs add {{{long} {Putrid zombie} {target} {zombie}}}; ...
  #var {area} {Section Z}                           - area label
  touch angel rune;                                 - one-time setup line(s)
  #act {...} {...}                                  - area-specific triggers

We pull out path + mobs + area + setup into muds/3k/bots/<name>.json. The
generic.tin LOOP itself is reimplemented client-side (the path-bot engine),
not transpiled - this only lifts the per-area data. Bots that carry bespoke
trigger logic (czodiacs/kayos/alphabet/findevent/findtv) are flagged
incomplete: their #act blocks are captured raw for a later hand-port.

Usage:  python tools/convert_tin_bots.py [--write]
Without --write it just reports; --write emits the JSON files.
"""

import json
import os
import re
import sys

SRC_DIR = os.path.join("supporting docs", "bots")
OUT_DIR = os.path.join("muds", "3k", "bots")
SKIP = {"generic.tin", "bot_cycle.tin"}


def split_braces(s):
    """Top-level {...} fields of a tintin record fragment. Adjacent groups
    ('{target}{elephant}') and nesting are handled; inner content kept raw."""
    fields, depth, cur = [], 0, []
    for ch in s:
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


def brace_arg(text, key):
    """Grab the {...} payload of '#var {key} {PAYLOAD}' (brace-balanced)."""
    m = re.search(r"#var\s*\{" + re.escape(key) + r"\}\s*\{", text)
    if not m:
        m = re.search(r"#var\s+" + re.escape(key) + r"\s+\{", text)
        if not m:
            return None
    i = m.end() - 1            # at the opening brace of the payload
    depth, start = 0, i
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:j]
    return None


def parse_path(payload):
    """Split a bot[path] payload into ordered steps on top-level ';'. A
    '{a;b}' group becomes one step 'a;b' (sent as a unit, not a room move)."""
    steps, depth, cur = [], 0, []
    for ch in payload:
        if ch == "{":
            depth += 1
            if depth == 1:
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                continue
        if ch == ";" and depth == 0:
            tok = "".join(cur).strip()
            if tok:
                steps.append(tok)
            cur = []
            continue
        cur.append(ch)
    tok = "".join(cur).strip()
    if tok:
        steps.append(tok)
    return steps


def parse_mobs(text):
    """Every '#list botmobs add {{{long} {NAME} {target} {KW}}}' -> entries.
    The leaf {..} fields never nest, so match them directly: the line yields
    [long, NAME, target, KW] -> match=NAME, target=KW."""
    mobs = []
    for line in text.splitlines():
        if "botmobs add" not in line:
            continue
        leaves = re.findall(r"\{([^{}]+)\}", line)
        kv = {}
        for i in range(0, len(leaves) - 1, 2):
            kv[leaves[i].strip().lower()] = leaves[i + 1].strip()
        name, target = kv.get("long"), kv.get("target")
        if name and target:
            mobs.append({"match": name, "target": target})
    return mobs


def parse_setup(text):
    """Top-level prep commands sent once before a run (the rolm 'touch ...
    rune' aggro line)."""
    setup = []
    for line in text.splitlines():
        s = line.strip().rstrip(";").strip()
        if re.match(r"^touch\s+\w+\s+rune$", s):
            setup.append(s)
    return setup


def parse_acts(text):
    """Raw #act / #action blocks - captured for later hand-porting only."""
    return re.findall(r"#(?:act|action)\b.*", text)


def titleize(stem):
    return re.sub(r"[-_]+", " ", stem).strip().title()


def convert(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    stem = os.path.splitext(os.path.basename(path))[0]
    payload = brace_arg(text, "bot[path]")
    steps = parse_path(payload) if payload else []
    mobs = parse_mobs(text)
    area = brace_arg(text, "area")
    msg = re.search(r"#var\s+bot_message_1\s+'([^']*)'", text)
    acts = parse_acts(text)
    has_wildcard = any("%" in mob["match"] for mob in mobs)
    incomplete = bool(acts) or has_wildcard
    rec = {
        "name": stem,
        "area": (area or titleize(stem)).strip(),
        "label": (msg.group(1).strip() if msg else ""),
        "setup": parse_setup(text),
        "loop": False,
        "path": steps,
        "mobs": mobs,
        "source": os.path.basename(path),
    }
    if incomplete:
        rec["_incomplete"] = True
        if acts:
            rec["_raw_acts"] = acts
        if has_wildcard:
            rec["_note"] = "wildcard mob match needs a regex port"
    return rec


def main():
    write = "--write" in sys.argv
    files = sorted(f for f in os.listdir(SRC_DIR)
                   if f.endswith(".tin") and f not in SKIP)
    if write:
        os.makedirs(OUT_DIR, exist_ok=True)
    print(f"{'bot':18} {'steps':>5} {'mobs':>4}  status")
    print("-" * 52)
    for fn in files:
        rec = convert(os.path.join(SRC_DIR, fn))
        status = "INCOMPLETE (triggers)" if rec.get("_incomplete") else "ok"
        print(f"{rec['name']:18} {len(rec['path']):>5} "
              f"{len(rec['mobs']):>4}  {status}")
        if write:
            out = os.path.join(OUT_DIR, rec["name"] + ".json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(rec, f, indent=2, ensure_ascii=False)
                f.write("\n")
    if not write:
        print("\n(dry run - pass --write to emit muds/3k/bots/*.json)")


if __name__ == "__main__":
    main()
