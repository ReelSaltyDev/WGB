"""What the Settings window changes, kept apart from the window itself.

Emulator hardware lives in each AVD's own config.ini (cores, RAM, GPU): those are
CEILINGS the emulator may use, not reservations. Real dedication is CPU pinning
and priority on the emulator's qemu process, which Windows forgets at every
launch, so the bot re-applies it after each boot.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Graphics. MEASURED 2026-09-17 (guarded runs: cold boot, same stream, 150s settle,
# run voided if the process or config.ini changed): writing hw.gpu.enabled /
# hw.gpu.mode to config.ini does NOTHING. With 'host', 'off' and
# 'swiftshader_indirect' in the file, Android reported the same renderer every
# time (the PC's NVIDIA card) and the same CPU use (1.98 / 1.98 / 2.04 cores).
# The emulator only obeys the -gpu launch flag. So the choice is kept in our own
# key and turned into that flag at boot.
#   what the window offers -> value for `emulator -gpu` (None = let the emulator decide)
GPU_MODES = {
    "default": None,                                 # the emulator picks; on a PC with a real GPU that is the GPU
    "host": "host",                                  # insist on the graphics card
    "software": "swiftshader_indirect",              # draw on the CPU: slower, for PCs whose picture misbehaves
}
GPU_KEY = "givvy.gpu"                                # our own line in config.ini; the emulator ignores unknown keys
PRIORITIES = {"normal": "Normal", "below_normal": "BelowNormal", "idle": "Idle", "above_normal": "AboveNormal"}
# Frame rate (hw.lcd.vsync). MEASURED 2026-09-17, one emulator playing a live stream, cold boots:
#   4 cores 60 fps 2.20 logical CPUs | 4 cores 30 fps 1.84 | 2 cores 60 fps 1.85 | 2 cores 30 fps 1.43
# with the bot's screen read going from 2.55s to about 2.8s. Things that did NOT help: a lower
# display size (`wm size`), and Windows efficiency mode (EcoQoS) on the emulator processes
# (137 W package power with it on and off).
FPS_KEY = "hw.lcd.vsync"
FPS_CHOICES = (60, 30)
HW_KEYS = ("hw.cpu.ncore", "hw.ramSize", GPU_KEY, FPS_KEY)


@dataclass
class Hardware:
    cores: int
    ram_mb: int
    gpu: str
    fps: int = 60


def avd_home() -> Path:
    return Path(os.environ.get("ANDROID_AVD_HOME") or Path.home() / ".android" / "avd")


def _ini(avd: str, home: Path | str | None) -> Path:
    return Path(home or avd_home()) / f"{avd}.avd" / "config.ini"


def _ram_mb(raw: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([gGmM]?)[bB]?\s*", raw)
    if not m:
        return 2048
    n = int(m.group(1))
    return n * 1024 if m.group(2).lower() == "g" else n


def read_hw(avd: str, avd_home: Path | str | None = None) -> Hardware:
    """The LAST occurrence of a key wins, which matters: our AVDs carry hw.ramSize
    twice (avdmanager's 2G, then the 4096 the installer appended)."""
    vals: dict[str, str] = {}
    for line in _ini(avd, avd_home).read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    gpu = vals.get(GPU_KEY, "default")
    if gpu not in GPU_MODES:
        gpu = "default"
    try:
        fps = int(float(vals.get(FPS_KEY, "60") or 60))
    except ValueError:
        fps = 60
    return Hardware(cores=int(vals.get("hw.cpu.ncore", "2") or 2), ram_mb=_ram_mb(vals.get("hw.ramSize", "2048")),
                    gpu=gpu, fps=fps if fps in FPS_CHOICES else 60)


def write_hw(avd: str, hw: Hardware, avd_home: Path | str | None = None, logical_cpus: int | None = None) -> None:
    cpus = logical_cpus or os.cpu_count() or 4
    if not 1 <= hw.cores <= min(16, cpus):
        raise ValueError(f"cores: 1 to {min(16, cpus)}")
    if not 1024 <= hw.ram_mb <= 16384:
        raise ValueError("RAM: 1024 to 16384 MB")
    if hw.gpu not in GPU_MODES:
        raise ValueError(f"GPU: one of {', '.join(GPU_MODES)}")
    if hw.fps not in FPS_CHOICES:
        raise ValueError(f"frame rate: one of {', '.join(map(str, FPS_CHOICES))}")
    path = _ini(avd, avd_home)
    keep = [l for l in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if l.split("=", 1)[0].strip() not in HW_KEYS]
    keep += [f"hw.cpu.ncore={hw.cores}", f"hw.ramSize={hw.ram_mb}", f"{GPU_KEY}={hw.gpu}", f"{FPS_KEY}={hw.fps}"]
    path.write_text("\n".join(keep) + "\n", encoding="utf-8")


def gpu_flag(avd: str, avd_home: Path | str | None = None) -> list[str]:
    """Launch arguments for the chosen graphics mode ([] = leave it to the emulator)."""
    try:
        value = GPU_MODES.get(read_hw(avd, avd_home).gpu)
    except Exception:
        value = None
    return ["-gpu", value] if value else []


# ---------------------------------------------------------------- CPU pinning
def affinity_mask(text: str, logical_cpus: int | None = None) -> int:
    """'0-3, 8' -> bit mask. Blank means not pinned (0)."""
    cpus = logical_cpus or os.cpu_count() or 64
    text = (text or "").strip()
    if not text:
        return 0
    mask = 0
    for part in text.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not m:
            raise ValueError(f"pin to cores: use numbers and ranges like 0-3, 8 (got {part.strip()!r})")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo > hi or hi >= min(cpus, 64):
            raise ValueError(f"pin to cores: this PC has logical processors 0 to {min(cpus, 64) - 1}")
        for i in range(lo, hi + 1):
            mask |= 1 << i
    return mask


def _powershell(script: str) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                       timeout=60, creationflags=NO_WINDOW)
    return r.stdout.strip()


def apply_process_tuning(avd: str, mask: int, priority: str = "normal", run=_powershell) -> int:
    """Pin / prioritise the running qemu process of ONE avd. Returns how many
    processes were touched (0 = that emulator is not running)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", avd):        # it goes into a PowerShell regex, unescaped
        raise ValueError(f"unexpected AVD name {avd!r}")
    prio = PRIORITIES.get(priority, "Normal")
    pin = f"$p.ProcessorAffinity = [IntPtr]{mask}L; " if mask else ""
    script = ("$n = 0; Get-CimInstance Win32_Process -Filter \"Name like 'qemu-system%'\" | "
              f"Where-Object {{ $_.CommandLine -match '-avd\\s+{avd}(\\s|$)' }} | "
              "ForEach-Object { $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
              f"if ($p) {{ {pin}$p.PriorityClass = '{prio}'; $n++ }} }}; $n")
    out = run(script)
    try:
        return int((out or "0").splitlines()[-1])
    except ValueError:
        return 0


# ---------------------------------------------------------------- config.toml edits
def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_config_value(path: Path | str, section: str, key: str, value) -> None:
    """Change one `key = value` inside `[section]`, keeping the line's comment and
    every other byte of the file."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    header = f"[{section}]"
    start = next((i for i, l in enumerate(lines) if l.strip() == header), None)
    if start is None:
        lines = lines + ([""] if lines and lines[-1].strip() else []) + [header, f"{key} = {_fmt(value)}", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    pat = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)([^#]*?)(\s*(#.*)?)$")
    for i in range(start + 1, end):
        m = pat.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{_fmt(value)}{m.group(3)}"
            break
    else:
        lines.insert(start + 1, f"{key} = {_fmt(value)}")
    path.write_text("\n".join(lines), encoding="utf-8")


def set_account_keys(path: Path | str, device_name: str, updates: dict) -> None:
    """Set keys inside the [[accounts]] block whose name is `device_name`."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    from .accounts import has_account_blocks
    if not has_account_blocks(text):
        # a fresh install implies its devices from [device]; write them out so there is a block to edit
        from .accounts import _block
        from .config import derive_accounts, load
        text = text.rstrip("\n") + "\n" + "".join(_block(a) for a in derive_accounts(load(path).device))
    lines = text.split("\n")
    starts = [i for i, l in enumerate(lines) if l.strip() == "[[accounts]]"]
    for s in starts:
        e = next((i for i in range(s + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        if not any(re.match(rf'\s*name\s*=\s*"{re.escape(device_name)}"', lines[i]) for i in range(s + 1, e)):
            continue
        block = lines[s:e]
        while block and not block[-1].strip():
            block.pop()
        trailing = lines[s + len(block):e]
        for key, value in updates.items():
            idx = next((i for i, l in enumerate(block) if re.match(rf"\s*{re.escape(key)}\s*=", l)), None)
            if idx is None:
                block.append(f"{key} = {_fmt(value)}")
            else:
                block[idx] = f"{key} = {_fmt(value)}"
        lines[s:e] = block + trailing
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    raise ValueError(f"no [[accounts]] block named {device_name!r} in {path.name}")
