# KatMUD v7

MIP-integrated MUD client for 3Scapes/3Kingdoms. Formerly pymud.
Tkinter, built on Windows (macOS support is new - see below), one
process per character.

## Install

Python 3.10+ from python.org (Tkinter included). One optional but
strongly recommended dependency:

    pip install keyring

Without it, passwords cannot be stored and the client prompts on
every connect.

## Migrate from pymud v6

    python tools\migrate_v6.py <path-to-old-pymud-folder>

This builds profiles.json, writes characters/<name>.json personal
layers (aliases, triggers, gags, numpad->keys, seen_max), copies
per-port landmark files to per-mud ones, and stores each profile's
password in Windows Credential Manager under katmud/<mud>/<character>.
**Delete the old pymud_profiles.json afterwards - it still contains
plaintext passwords.** Guild files do NOT migrate (v6's per-port
format is incompatible); re-author them under muds/<mud>/guilds/
using muds/3s/guilds/vikings.json as the section reference.

## Run

    katmud.pyw                 -> character picker
    katmud.pyw 3s-normal       -> that profile directly
                                  (make per-character shortcuts)

The picker spawns each client as a detached process and exits. A
crash in one character can never take down another. Startup failures
land in logs/crash.log (pythonw has no console).

## Running on macOS (untested)

The client is plain Python + Tkinter, so it should run on a Mac, but
it has only ever been played on Windows. The Mac-specific pieces -
tell sounds (via the built-in `afplay`), right-click / Ctrl-click
menus, Cmd +/- font size, and the Menlo monospace font - were written
without a Mac to test on.

1. Install Python 3.10+ from python.org. Use that installer, not
   Homebrew's Python: it bundles a working Tk.
2. In Terminal:

        pip3 install keyring
        cd /path/to/KATMud
        python3 katmud.pyw

   Passwords go in the macOS Keychain.

Expect cosmetic rough edges: macOS ignores background colours on
standard Tk buttons, so some coloured buttons may show grey. If you
run it on a Mac, reports of what looks or works wrong are very
welcome - screenshots plus the contents of logs/crash.log help most.

## Configuration cascade

Load order, later wins, collisions REPLACE:

    global.json
    muds/<mud>/mud.json
    muds/<mud>/guilds/<guild>.json    (skipped when guild = none)
    characters/<character>.json

Discipline rule: guild-specific config lives in guild files, never in
character files, or guild-switching breaks its promise. The builder
(Tools > Aliases & Triggers) and Keybindings dialog write into any
layer; hand-editing the json files is equally valid - unknown keys
and ordering are preserved.

`#help` in the client lists commands; **docs/COMMANDS.md** is the full
reference for every command the client recognizes. `#map on/off/here/rate` and
`#record [scope]` drive the mapping system; mapping mode auto-engages
when you walk off the known map (disable: settings.auto_mapping
false in any layer).

## Verified against live output

Rating capture (AREA NAME / AREA RATING -> / Monster class range,
including the name-less overland response and [House] detection) is
confirmed against live 3s captures, 2026-06-12.

## Needs live verification

1. **Auth-failure detection** matches /wrong|incorrect password/i.
   If 3s words rejection differently, the re-prompt won't trigger -
   capture the real line and adjust AUTH_FAIL_RE in
   katmud_lib/client.py.
2. **3k speedruns** file is intentionally header-only: the shared
   file's room ids were authored against the 3s map and are wrong on
   3k's id space. Rebuild it when the new 3k map exists
   (tools/split_speedruns.py --trust-ids to override).
