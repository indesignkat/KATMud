#!/usr/bin/env python3
"""katmud.pyw - KatMUD entry point (formerly pymud).

No arguments      -> character picker (transient launcher)
<profile-id> arg  -> client for that profile, directly
                     (enables per-character desktop shortcuts)

The .pyw extension makes Windows launch this under pythonw.exe - no
console window. pythonw discards stdout/stderr, so EVERYTHING from
import time onward sits inside the crash safety net below: a failure
writes the full traceback to logs/crash.log and attempts a bare
tkinter messagebox before exiting (spec 1.1).
"""

import os
import sys
import time
import traceback


def _crash(exc):
    base = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base, "logs")
    text = "".join(traceback.format_exception(type(exc), exc,
                                              exc.__traceback__))
    header = (f"KatMUD crash {time.strftime('%Y-%m-%d %H:%M:%S')} "
              f"argv={sys.argv!r}\n")
    path = os.path.join(log_dir, "crash.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(header + text + "\n")
    except OSError:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "KatMUD crashed",
            f"{type(exc).__name__}: {exc}\n\nFull traceback written "
            f"to:\n{path}")
        root.destroy()
    except Exception:
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--hub":
        from katmud_lib import hub
        hub.run()
    elif len(sys.argv) > 1:
        profile_id = sys.argv[1]
        from katmud_lib import client
        client.run(profile_id)
    else:
        from katmud_lib import picker
        picker.run()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:        # noqa: BLE001 - this IS the net
        _crash(e)
        sys.exit(1)
