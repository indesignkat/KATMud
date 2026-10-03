# KatMUD v7

GMCP-enabled MUD client for 3Scapes. Tkinter, built on Windows (macOS
support is new - see below), one process per character.

Guild support is being built out guild by guild. Updated so far:
Bladesingers, Vikings, Angels, Necromancers and Elementals. The rest
of the guilds are coming soon.

3Scapes sends real room vnums, so KatMUD builds its own map from
scratch as you play (muds/3s/3s.db). The map is still growing - see
"Mapping — sqlite backend" in docs/COMMANDS.md for charting new rooms.

## Install

Python 3.10+ from python.org (Tkinter included). One optional but
strongly recommended dependency:

    pip install keyring

Without it, passwords cannot be stored and the client prompts on
every connect.

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

   Passwords go in the macOS Keychain. The first time the client
   saves or reads one, macOS may ask whether Python can use the
   Keychain - "Always Allow" stops it asking again.

Expect cosmetic rough edges: macOS ignores background colours on
standard Tk buttons, so some coloured buttons may show grey. If you
run it on a Mac, reports of what looks or works wrong are very
welcome - screenshots plus the contents of logs/crash.log help most.

## Configuration

Settings live in JSON layers, loaded in this order. A later layer
wins, and an entry with the same name REPLACES the earlier one (it
does not merge):

    global.json                                everyone
    muds/3s/mud.json                           the whole mud
    muds/3s/guilds/<guild>.json                one guild
    characters/<character>.json                one character name
    characters/<character>-3s-<guild>.json     one character in one
                                               guild

Put each thing in the narrowest layer that fits. Guild-specific
settings (e.g. a corpse routine) belong in the last one, so they
don't follow the character into a different guild.

Tools > Aliases & Triggers and Tools > Keybindings can write to any
layer, and hand-editing the files is fine too - unknown keys and
their order are preserved.

`#help` lists commands in the client; **docs/COMMANDS.md** is the full
reference for every command KatMUD recognizes.
