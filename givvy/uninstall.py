"""`WhatnotGivvy.exe --uninstall-cleanup`: run by the uninstaller BEFORE it removes the program.

Windows' own uninstall (Settings > Apps) removes the program folder, shortcuts and
the registry entry. What it cannot know about lives outside that folder: the
emulators (which hold the Whatnot sign-ins) and the Android SDK Google's tool
downloaded. This asks, then removes exactly what was agreed to.

THE ACCIDENT THIS FILE IS SHAPED BY (2026-09-18). A test uninstall on the author's
own PC deleted the small settings files of his real main emulator; the big files
were locked by the running emulator and survived, but on the next boot the emulator
found its metadata gone and rebuilt the data disk: his Google and Whatnot sign-in
were lost for good. Three things allowed it, and each is now closed:
  1. any emulator merely NAMED in config.toml counted as ours. Now only an emulator
     carrying the marker file create_avd() leaves inside it is ever offered;
  2. the boxes came pre-ticked and closing the window counted as consent. Now
     nothing is ticked, and only the 'Delete what I ticked' button deletes;
  3. files were deleted one by one while the emulator still held others open. Now an
     emulator is either renamed away whole (proving nothing holds it) or left whole.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)
SDK_MARKER = ".created-by-whatnot-givvy"        # written when WE created the SDK folder
AVD_MARKER = ".created-by-whatnot-givvy"        # written by create_avd() inside the <name>.avd folder
DEFAULT_TICKED = False
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def decision_when_window_closed() -> dict:
    """Closing the dialog is not consent."""
    return {"emulators": False, "sdk": False}


def plan(cfg, avd_home: Path) -> dict:
    """What exists that could be removed. Pure: touches nothing."""
    sdk = Path(cfg.device.android_sdk)
    ours, foreign = [], []
    for a in cfg.accounts:
        if a.kind == "emulator" and a.avd:
            d, ini = avd_home / f"{a.avd}.avd", avd_home / f"{a.avd}.ini"
            if d.exists() or ini.exists():
                (ours if (d / AVD_MARKER).exists() else foreign).append((a.avd, a.port, d, ini))
    return {"avds": ours, "avds_foreign": foreign,
            "sdk": sdk if (sdk / SDK_MARKER).exists() else None,
            "sdk_foreign": sdk if sdk.exists() and not (sdk / SDK_MARKER).exists() else None}


def stop_emulators(cfg, avds) -> None:
    """Only the emulators about to be deleted, asked to shut down cleanly."""
    from .device.adb import adb_path
    adb = adb_path(cfg.device.android_sdk)
    for _avd, port, _d, _ini in avds:
        try:
            subprocess.run([adb, "-s", f"emulator-{port}", "emu", "kill"], capture_output=True, timeout=30,
                           creationflags=NO_WINDOW)
        except Exception:
            pass
    if avds:
        time.sleep(8)


def _can_delete(d: Path) -> bool:
    """True only if NOTHING holds the folder: renaming a directory fails while any file in it is open."""
    if not d.exists():
        return True
    probe = d.with_name(d.name + ".deleting")
    try:
        d.rename(probe)
        probe.rename(d)
        return True
    except OSError:
        return False


def remove(p: dict, emulators: bool, sdk: bool, can_delete=_can_delete) -> list[str]:
    done = []
    if emulators:
        for avd, _port, d, ini in p["avds"]:
            if not can_delete(d):
                log.warning("emulator %s is still in use; leaving it completely alone", avd)
                continue                             # whole or not at all: a half-deleted emulator loses its data
            shutil.rmtree(d, ignore_errors=True)
            ini.unlink(missing_ok=True)
            done.append(f"emulator {avd}")
    if sdk and p["sdk"] is not None and can_delete(p["sdk"]):
        shutil.rmtree(p["sdk"], ignore_errors=True)
        parent = p["sdk"].parent
        try:
            if parent.name.lower() == "android" and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
        done.append(f"Android SDK {p['sdk']}")
    return done


def main(argv: list[str]) -> int:
    from .avd_settings import avd_home
    from .config import load
    try:
        cfg = load("config.toml")
    except Exception:
        return 0                                     # never configured: nothing of ours outside the folder
    p = plan(cfg, avd_home())
    if "--silent" in argv or (not p["avds"] and p["sdk"] is None):
        return 0                                     # nobody to ask, or nothing of ours: leave everything as it is
    import tkinter as tk
    from tkinter import ttk
    chosen = decision_when_window_closed()
    root = tk.Tk(); root.title("Uninstall Whatnot Givvy"); root.resizable(False, False)
    frm = ttk.Frame(root, padding=16); frm.pack()
    ttk.Label(frm, text="The program itself is being removed. These were created by it, outside its folder.\n"
                        "Nothing is deleted unless you tick it.", font=("Segoe UI", 10, "bold"), justify="left").pack(anchor="w")
    v_emu, v_sdk = tk.BooleanVar(value=DEFAULT_TICKED), tk.BooleanVar(value=DEFAULT_TICKED)
    if p["avds"]:
        names = ", ".join(a[0] for a in p["avds"])
        ttk.Checkbutton(frm, variable=v_emu, text=f"Delete the emulators ({names}). The Google and Whatnot sign-ins inside "
                                                  f"them are lost for good.").pack(anchor="w", pady=(10, 0))
    if p["sdk"] is not None:
        ttk.Checkbutton(frm, variable=v_sdk, text=f"Delete the Android SDK this program downloaded ({p['sdk']}, about 3 GB)."
                        ).pack(anchor="w", pady=(6, 0))
    for label, items in (("emulator", [a[0] for a in p["avds_foreign"]]), ("folder", [str(p["sdk_foreign"])] if p["sdk_foreign"] else [])):
        for it in items:
            ttk.Label(frm, text=f"The {label} {it} was not created by this program and is left alone.",
                      foreground="#555").pack(anchor="w", pady=(6, 0))

    def delete():
        chosen.update(emulators=bool(v_emu.get()) and bool(p["avds"]), sdk=bool(v_sdk.get()))
        root.destroy()
    row = ttk.Frame(frm); row.pack(anchor="e", pady=(14, 0))
    ttk.Button(row, text="Keep everything", command=root.destroy).pack(side="right")
    ttk.Button(row, text="Delete what I ticked", command=delete).pack(side="right", padx=8)
    root.protocol("WM_DELETE_WINDOW", root.destroy)      # closing the window keeps everything
    root.mainloop()
    if chosen["emulators"]:
        stop_emulators(cfg, p["avds"])
    remove(p, chosen["emulators"], chosen["sdk"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
