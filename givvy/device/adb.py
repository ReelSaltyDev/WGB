"""Thin adb wrapper."""
from __future__ import annotations

import subprocess
from pathlib import Path


def adb_path(sdk: str) -> str:
    p = Path(sdk) / "platform-tools" / "adb.exe"
    return str(p) if p.exists() else "adb"


def parse_devices(output: str) -> dict[str, str]:
    out = {}
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def list_devices(sdk: str) -> dict[str, str]:
    r = subprocess.run([adb_path(sdk), "devices", "-l"], capture_output=True, text=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return parse_devices(r.stdout)


def shell(sdk: str, serial: str, *args: str, timeout: int = 30) -> str:
    r = subprocess.run([adb_path(sdk), "-s", serial, "shell", *args], capture_output=True, text=True,
                       timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout


def exec_out(sdk: str, serial: str, *args: str, timeout: int = 60) -> bytes:
    """Raw bytes from the device, with no newline translation (screencap, cat)."""
    r = subprocess.run([adb_path(sdk), "-s", serial, "exec-out", *args], capture_output=True,
                       timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout


def emu_kill(sdk: str, serial: str, timeout: int = 30) -> str:
    """Ask an emulator to shut itself down through its console. This is the clean
    way: it flushes the disk image and saves quick-boot state before exiting."""
    r = subprocess.run([adb_path(sdk), "-s", serial, "emu", "kill"], capture_output=True, text=True,
                       timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout
