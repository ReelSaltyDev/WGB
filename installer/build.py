"""Build dist/WhatnotGivvySetup.exe.   Run:  .venv\\Scripts\\python installer\\build.py

  1. PyInstaller builds the app as a FOLDER (dist/WhatnotGivvy). Not "one-file": a
     one-file exe unpacks itself into TEMP and runs from there, which is the shape
     antivirus heuristics call a trojan. A friend's Defender did exactly that.
  2. The folder is audited: nothing personal may ship.
  3. Inno Setup wraps it into a standard Windows installer (uninstall entry, upgrades).
  4. Windows Defender scans the results; a detection fails the build.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD, DIST = ROOT / "build", ROOT / "dist"
FORBIDDEN_FILES = {"config.toml", "state.db", "ui_state.json", "givvy.log"}
ISCC_PLACES = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
               Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"), Path("C:/Program Files/Inno Setup 6/ISCC.exe"))


def version() -> str:
    sys.path.insert(0, str(ROOT))
    from givvy.version import __version__
    return __version__


def pyinstaller(*args: str) -> None:
    print(">", " ".join(args))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noupx",
                    "--workpath", str(BUILD), "--specpath", str(BUILD), *args], check=True, cwd=ROOT)


def personal_strings() -> list[str]:
    """Whatever is in the author's real config must never appear in the payload."""
    import tomllib
    cfg = ROOT / "config.toml"
    if not cfg.exists():
        return []
    raw = tomllib.loads(cfg.read_text(encoding="utf-8"))
    d, dev = raw.get("discord", {}), raw.get("device", {})
    vals = [d.get("bot_token"), d.get("channel_id"), d.get("mention", "").strip("<@>"), dev.get("phone_serial")]
    vals += [a.get("serial") for a in raw.get("accounts", [])]
    return sorted({v for v in vals if v and len(v) >= 6})


def audit(payload: Path) -> None:
    secrets = [s.encode() for s in personal_strings()]
    for p in payload.rglob("*"):
        if p.is_file():
            if p.name in FORBIDDEN_FILES:
                raise SystemExit(f"REFUSING TO BUILD: {p} must not ship")
            if p.stat().st_size < 30_000_000:
                data = p.read_bytes()
                for s in secrets:
                    if s in data:
                        raise SystemExit(f"REFUSING TO BUILD: personal value found inside {p}")
    print(f"audit: clean ({len(secrets)} personal values checked)")


def stamp() -> str:
    """Record exactly which commit is being packaged; shown in Settings."""
    import datetime
    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    commit, dirty = git("rev-parse", "--short", "HEAD"), bool(git("status", "--porcelain", "--untracked-files=no"))
    date = datetime.date.today().isoformat()
    (ROOT / "givvy" / "_build.py").write_text(f'COMMIT = "{commit}"\nDATE = "{date}"\nDIRTY = {dirty}\n', encoding="utf-8")
    if dirty:
        print("WARNING: uncommitted changes are being packaged")
    return f"{commit}{'+changes' if dirty else ''} {date}"


def defender_scan(*paths: Path) -> None:
    mp = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Windows Defender" / "MpCmdRun.exe"
    if not mp.exists():
        print("Defender not found; skipping the scan")
        return
    for p in paths:
        r = subprocess.run([str(mp), "-Scan", "-ScanType", "3", "-File", str(p), "-DisableRemediation"],
                           capture_output=True, text=True)
        verdict = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else f"exit {r.returncode}"
        print(f"Defender: {p.name}: {verdict}")
        if r.returncode == 2 or "found no threats" not in (r.stdout or ""):
            raise SystemExit(f"REFUSING TO PUBLISH: Windows Defender flagged {p}")


def main(app_id_suffix: str = "") -> Path:
    iscc = next((p for p in ISCC_PLACES if p.exists()), None)
    if iscc is None:
        raise SystemExit("Inno Setup 6 is not installed (winget install JRSoftware.InnoSetup)")
    for d in (BUILD, DIST):
        shutil.rmtree(d, ignore_errors=True)
    stamped, ver = stamp(), version()
    icon = ROOT / "givvy" / "assets" / "givvy.ico"
    try:
        pyinstaller("--windowed", "--name", "WhatnotGivvy", "--distpath", str(DIST), "--icon", str(icon),
                    "--add-data", f"{icon};givvy/assets",
                    "--add-data", f"{icon.parent / 'timebomb-logo.png'};givvy/assets",
                    "--hidden-import", "givvy.themes", "--hidden-import", "givvy.blast",
                    "--hidden-import", "givvy.settings_window", "--hidden-import", "givvy.check_tab",
                    "--hidden-import", "givvy.uninstall", "--hidden-import", "givvy._build",
                    "--hidden-import", "givvy.device.reader", "--hidden-import", "givvy.metrics", "--hidden-import", "givvy.replay",
                    "--hidden-import", "givvy.wincharts", "--hidden-import", "givvy.screens",
                    # the persistent screen reader: its u2.jar is pushed to each emulator from the package's assets
                    "--collect-all", "uiautomator2", "--collect-submodules", "adbutils",
                    str(ROOT / "installer" / "app_entry.py"))
    finally:
        (ROOT / "givvy" / "_build.py").unlink(missing_ok=True)      # the source tree reports itself as 'source'
    payload = DIST / "WhatnotGivvy"
    shutil.copy(ROOT / "config.example.toml", payload / "config.example.toml")
    shutil.copy(ROOT / "README.md", payload / "README.md")
    audit(payload)
    defines = [f"/DAppVer={ver}", f"/DSrc={payload}", f"/DOut={DIST}"]
    if app_id_suffix:
        defines.append(f"/DAppIdSuffix={app_id_suffix}")
    subprocess.run([str(iscc), "/Qp", *defines, str(ROOT / "installer" / "givvy.iss")], check=True, cwd=ROOT)
    out = DIST / "WhatnotGivvySetup.exe"
    defender_scan(payload / "WhatnotGivvy.exe", out)
    import hashlib
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    (DIST / "WhatnotGivvySetup.exe.sha256").write_text(f"{sha}  WhatnotGivvySetup.exe\n", encoding="ascii")
    print(f"\nbuilt {out}  ({out.stat().st_size / 1e6:.0f} MB)  version {ver} from {stamped}\nsha256 {sha}")
    return out


if __name__ == "__main__":
    main(next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--app-id-suffix=")), ""))
