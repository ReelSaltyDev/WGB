"""Fetch the Android SDK pieces the bot needs, straight from Google.

The installer must NOT ship these: the Android SDK licence forbids
redistribution, and the Play Store system image has its own terms. So the person
installing accepts Google's licences themselves and their PC downloads the files
from Google, exactly as tools/install_emulator.ps1 does on the author's machine.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

IMAGE = "system-images;android-35;google_apis_playstore;x86_64"
PACKAGES = ["platform-tools", "emulator", "platforms;android-35", IMAGE]
JDK_URL = "https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jdk/hotspot/normal/eclipse"
STUDIO_PAGE = "https://developer.android.com/studio"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) givvy-setup"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Progress = Callable[[str], None]


def find_cmdline_tools_url(html: str) -> str | None:
    m = re.search(r"https://dl\.google\.com/android/repository/commandlinetools-win-\d+_latest\.zip", html)
    return m.group(0) if m else None


def download(url: str, dest: Path, say: Progress, label: str) -> None:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, dest.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else -1
            if pct // 10 != last // 10 or not total and done % (50 << 20) < (1 << 20):
                last = pct
                say(f"{label}: {done >> 20} MB" + (f" of {total >> 20} MB" if total else ""))


def _env(sdk: Path) -> dict:
    env = dict(os.environ)
    jdk = sdk / "jdk"
    if (jdk / "bin" / "java.exe").exists():
        env["JAVA_HOME"] = str(jdk)
        env["PATH"] = str(jdk / "bin") + os.pathsep + env.get("PATH", "")
    env["ANDROID_HOME"] = env["ANDROID_SDK_ROOT"] = str(sdk)
    return env


def ensure_jdk(sdk: Path, say: Progress) -> None:
    if (sdk / "jdk" / "bin" / "java.exe").exists():
        say("Java: already there")
        return
    with tempfile.TemporaryDirectory() as td:
        z = Path(td) / "jdk.zip"
        download(JDK_URL, z, say, "Java (needed by Google's installer)")
        say("Java: unpacking...")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(Path(td) / "x")
        inner = next(p for p in (Path(td) / "x").iterdir() if p.is_dir())
        shutil.move(str(inner), str(sdk / "jdk"))


def ensure_cmdline_tools(sdk: Path, say: Progress) -> None:
    if (sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat").exists():
        say("Android command-line tools: already there")
        return
    req = urllib.request.Request(STUDIO_PAGE, headers=UA)
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    url = find_cmdline_tools_url(html)
    if not url:
        raise RuntimeError("could not find Google's command-line tools download link")
    with tempfile.TemporaryDirectory() as td:
        z = Path(td) / "clt.zip"
        download(url, z, say, "Android command-line tools")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(Path(td) / "x")
        (sdk / "cmdline-tools").mkdir(parents=True, exist_ok=True)
        shutil.move(str(Path(td) / "x" / "cmdline-tools"), str(sdk / "cmdline-tools" / "latest"))


def sdkmanager(sdk: Path, args: list[str], say: Progress, stdin: str | None = None) -> int:
    exe = sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
    p = subprocess.Popen([str(exe), f"--sdk_root={sdk}", *args], env=_env(sdk), text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=NO_WINDOW)
    if stdin is not None:
        try:
            p.stdin.write(stdin)
        except OSError:
            pass
    p.stdin.close()
    last = ""
    for line in p.stdout:                     # progress bars arrive as \r-separated updates
        for part in line.replace("\r", "\n").split("\n"):
            part = part.strip()
            m = re.search(r"(\d+)%\s*(.*)", part)
            key = (m.group(1)[:-1] + m.group(2)[:24]) if m else part
            if part and key != last:
                last = key
                if m or "Warning" not in part:
                    say("  " + part[:110])
    return p.wait()


def packages_present(sdk: Path) -> bool:
    return ((sdk / "platform-tools" / "adb.exe").exists() and (sdk / "emulator" / "emulator.exe").exists()
            and (sdk / "system-images" / "android-35" / "google_apis_playstore" / "x86_64" / "system.img").exists())


def ensure_sdk(sdk: Path | str, say: Progress) -> None:
    """Everything up to (not including) the AVD. Safe to re-run; skips what exists."""
    sdk = Path(sdk)
    ours = not sdk.exists() or not any(sdk.iterdir())
    sdk.mkdir(parents=True, exist_ok=True)
    if ours:
        (sdk / ".created-by-whatnot-givvy").write_text("Created by Whatnot Givvy; its uninstaller may delete this folder.\n",
                                                       encoding="utf-8")
    if packages_present(sdk) and (sdk / "cmdline-tools" / "latest").exists():
        say("Android emulator and system image: already installed")
        ensure_jdk(sdk, say)
        return
    ensure_jdk(sdk, say)
    ensure_cmdline_tools(sdk, say)
    say("Recording your acceptance of Google's SDK licences...")
    sdkmanager(sdk, ["--licenses"], lambda s: None, stdin="y\n" * 40)
    say("Downloading the emulator and Android image from Google (about 2 GB, be patient)...")
    rc = sdkmanager(sdk, PACKAGES, say)
    if rc != 0 or not packages_present(sdk):
        raise RuntimeError(f"Google's sdkmanager failed (exit {rc}). Check your connection and run Setup again.")


def accel_check(sdk: Path | str) -> tuple[bool, str]:
    """Can the emulator use hardware acceleration? Without it, it is unusably slow."""
    exe = Path(sdk) / "emulator" / "emulator.exe"
    try:
        r = subprocess.run([str(exe), "-accel-check"], capture_output=True, text=True, timeout=60,
                           env=_env(Path(sdk)), creationflags=NO_WINDOW)
    except Exception as e:
        return False, str(e)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0 and "is installed and usable" in out, out


def fresh_config(example_text: str, sdk: Path | str) -> str:
    """config.toml for a new install: the example with the SDK path filled in and
    nothing of the author's in it (no phone serial, no Discord mention, no sellers)."""
    sdk_s = str(sdk).replace("\\", "/")
    t = example_text
    t = re.sub(r'(?m)^android_sdk\s*=.*$', f'android_sdk = "{sdk_s}"', t)
    t = re.sub(r'(?m)^phone_serial\s*=.*$', 'phone_serial = ""', t)
    t = re.sub(r'(?m)^mention\s*=.*$', 'mention = ""', t)
    t = re.sub(r'(?m)^bot_token\s*=.*$', 'bot_token = ""', t)
    t = re.sub(r'(?m)^channel_id\s*=.*$', 'channel_id = ""', t)
    t = re.sub(r'(?m)^alerts_channel_id\s*=.*$', 'alerts_channel_id = ""', t)
    t = re.sub(r'(?m)^followed_sellers\s*=.*$', 'followed_sellers = []', t)
    return t
