# KatMUD v7

MIP-integrated MUD client for MUDs that speak 3k-style MIP (built and
tested against 3Scapes). Tkinter, Windows-native, one process per
character.

This is the public/private companion repo: it ships the client engine
and the 3Scapes (`3s`) configuration, with no personal character data.
`profiles.json` ships empty (see the comment in that file for the
schema) and `characters/` ships only `_TEMPLATE_character.json` - copy
it to `characters/<name>.json` per character, or let the picker
scaffold one on profile save.

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

`#help` in the client lists commands. `#map on/off/here/rate` and
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
2. **Adding another mud**: copy `muds/_TEMPLATE_mud.json` into a new
   `muds/<mudname>/mud.json` and fill in connection details; the
   client runs maplessly until you bootstrap a map with `#map new`.
