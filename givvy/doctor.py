"""Why won't the emulator come up? Ordered checks with a repair for each.

Written after a friend's install: Setup finished, the app opened, Start was pressed
and no emulator ever appeared, with no reason given anywhere. On a PC that has
never run an Android emulator the usual cause is that Windows Hypervisor Platform
is off (it is off by default), so the emulator exits at once.

Order matters: each check only means something once the ones before it pass.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VC_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"      # Microsoft's own permalink


@dataclass
class Check:
    key: str            # "bios" | "hypervisor" | "vcredist" | "sdk" | "avd:<name>" | "disk" | "ram"
    title: str
    ok: bool | None     # None = could not check
    detail: str
    fix: str = ""       # which repair the window should offer: "" | "hypervisor" | "vcredist" | "setup"


# ---------------------------------------------------------------- probes (all work without admin)
def _ps(script: str) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                       timeout=60, creationflags=NO_WINDOW)
    return r.stdout.strip()


def firmware_virtualization() -> bool:
    return _ps("(Get-CimInstance Win32_Processor | Select-Object -First 1).VirtualizationFirmwareEnabled").lower() == "true"


def hypervisor_platform() -> bool:
    out = _ps("(Get-CimInstance Win32_OptionalFeature -Filter \"Name='HypervisorPlatform'\").InstallState")
    return out.strip() == "1"


def vc_runtime_missing() -> list[str]:
    sys32 = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32"
    return [d for d in VC_DLLS if not (sys32 / d).exists()]


def sdk_missing(sdk: str) -> list[str]:
    p = Path(sdk)
    parts = {"adb": p / "platform-tools" / "adb.exe", "emulator": p / "emulator" / "emulator.exe",
             "Android system image": p / "system-images" / "android-35" / "google_apis_playstore" / "x86_64" / "system.img"}
    return [name for name, f in parts.items() if not f.exists()]


def avd_exists(avd: str) -> bool:
    from .avd_settings import avd_home
    return (avd_home() / f"{avd}.avd" / "config.ini").exists()


def free_gb(path: str) -> float:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / 2**30


def ram_gb() -> float:
    import ctypes

    class MS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong)] + [(f"x{i}", ctypes.c_ulonglong) for i in range(5)]
    ms = MS(); ms.l = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    return ms.total / 2**30


PROBES = dict(firmware_virtualization=firmware_virtualization, hypervisor_platform=hypervisor_platform,
              vc_runtime_missing=vc_runtime_missing, sdk_missing=sdk_missing, avd_exists=avd_exists,
              free_gb=free_gb, ram_gb=ram_gb)


# ---------------------------------------------------------------- the checks
def run(sdk: str, avds: list[str], probes: dict | None = None) -> list[Check]:
    p = dict(PROBES)
    p.update(probes or {})
    out: list[Check] = []

    def add(key, title, fn, good, fix=""):
        try:
            ok, detail = fn()
            out.append(Check(key, title, ok, good if ok else detail, "" if ok else fix))
        except Exception as e:
            out.append(Check(key, title, None, f"Could not check this ({e})."))

    add("bios", "CPU virtualisation is switched on in the BIOS",
        lambda: (p["firmware_virtualization"](),
                 "Off. Restart the PC, enter the BIOS/UEFI setup and enable 'SVM Mode' (AMD) or 'Intel VT-x / "
                 "Virtualization Technology'. Nothing else here can work until this is on."), "On.")
    add("hypervisor", "Windows Hypervisor Platform is enabled",
        lambda: (p["hypervisor_platform"](),
                 "Off (Windows ships with it off). The emulator exits at once without it. The button turns it on; "
                 "Windows asks for administrator permission, and the PC must be restarted afterwards."),
        "Enabled.", fix="hypervisor")

    def vc():
        missing = p["vc_runtime_missing"]()
        return (not missing, f"Missing {', '.join(missing)}. The button installs Microsoft's Visual C++ runtime "
                             f"(a small download from microsoft.com; Windows asks for administrator permission).")
    add("vcredist", "Microsoft Visual C++ runtime is installed", vc, "Installed.", fix="vcredist")

    def sdkc():
        missing = p["sdk_missing"](sdk)
        return (not missing, f"Not downloaded yet: {', '.join(missing)}. The button downloads them from Google (about 2 GB).")
    add("sdk", "Android emulator and system image are downloaded", sdkc, f"In {sdk}.", fix="setup")

    for avd in avds:
        add(f"avd:{avd}", f"Emulator '{avd}' has been created",
            lambda avd=avd: (p["avd_exists"](avd), f"'{avd}' does not exist yet. The button creates it."),
            "Created.", fix="setup")

    def disk():
        gb = p["free_gb"](sdk)
        return (gb >= 8, f"Only {gb:.0f} GB free on that drive; an emulator needs about 8 GB. Free some space.")
    add("disk", "Enough free disk space", disk, "Plenty.")

    def ram():
        gb = p["ram_gb"]()
        return (gb >= 7.5, f"This PC has {gb:.0f} GB of RAM. One emulator needs about 5 GB to itself, so it will be "
                           f"slow or fail to start.")
    add("ram", "Enough memory", ram, "Plenty.")
    return out


def first_problem(checks: list[Check]) -> Check | None:
    return next((c for c in checks if c.ok is False), None)


# ---------------------------------------------------------------- repairs that need administrator permission
def enable_hypervisor_elevated() -> None:
    """Windows shows its own UAC prompt; a restart is needed afterwards."""
    _ps("Start-Process dism.exe -Verb RunAs -ArgumentList "
        "'/online','/enable-feature','/featurename:HypervisorPlatform','/all','/norestart'")


def install_vc_redist(say=lambda m: None) -> bool:
    """Download Microsoft's redistributable from microsoft.com and run it (UAC prompt)."""
    import tempfile
    import urllib.request
    dest = Path(tempfile.gettempdir()) / "vc_redist.x64.exe"
    say("Downloading Microsoft Visual C++ runtime from microsoft.com...")
    req = urllib.request.Request(VC_REDIST_URL, headers={"User-Agent": "givvy-setup"})
    with urllib.request.urlopen(req, timeout=120) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)
    say("Running Microsoft's installer...")
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"(Start-Process -FilePath '{dest}' -ArgumentList '/install','/passive','/norestart' "
                        f"-Verb RunAs -Wait -PassThru).ExitCode"], capture_output=True, text=True, creationflags=NO_WINDOW)
    return r.stdout.strip() in ("0", "3010", "1638")      # ok / ok, reboot needed / newer version already there
