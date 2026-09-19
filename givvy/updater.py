"""Update from GitHub Releases, when the person presses the button.

A release is a tag like v1.4.0 with two files attached: WhatnotGivvySetup.exe and
WhatnotGivvySetup.exe.sha256. Nothing installs by itself: the app checks, says what
is available, and only downloads and runs the installer when asked. The download is
HTTPS from github.com and its SHA-256 must match the published one before it runs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
ASSET = "WhatnotGivvySetup.exe"
UA = {"User-Agent": "WhatnotGivvy-updater", "Accept": "application/vnd.github+json"}


@dataclass
class Release:
    version: str
    notes: str
    url: str
    sha256: str
    size: int
    page: str


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.4.0' -> (1, 4, 0). Anything unparseable sorts lowest, so it never looks newer."""
    m = re.fullmatch(r"\s*v?(\d+(?:\.\d+)*)\s*", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _get(url: str, timeout: int = 30) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("refusing a non-HTTPS update URL")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def latest(repo: str, fetch=_get) -> Release | None:
    """The newest published release of owner/name, or None if there is none usable."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        return None
    data = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/latest"))
    assets = {a["name"]: a for a in data.get("assets", [])}
    exe = assets.get(ASSET)
    if not exe:
        return None
    sha = ""
    digest = exe.get("digest") or ""                   # GitHub publishes this itself: "sha256:..."
    if digest.startswith("sha256:"):
        sha = digest.split(":", 1)[1].strip().lower()
    elif ASSET + ".sha256" in assets:
        sha = fetch(assets[ASSET + ".sha256"]["browser_download_url"]).decode("ascii", "replace").split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None                                    # no checksum, no update: never run an unverified download
    return Release(version=data.get("tag_name", ""), notes=(data.get("body") or "").strip(),
                   url=exe["browser_download_url"], sha256=sha, size=int(exe.get("size") or 0),
                   page=data.get("html_url", ""))


def check(repo: str, current: str, fetch=_get) -> Release | None:
    """A release newer than `current`, else None."""
    rel = latest(repo, fetch)
    return rel if rel and is_newer(rel.version, current) else None


def download(rel: Release, progress=lambda done, total: None, dest_dir: Path | None = None) -> Path:
    if not rel.url.startswith("https://github.com/"):
        raise ValueError("refusing an update that is not hosted on github.com")
    dest = Path(dest_dir or tempfile.gettempdir()) / f"WhatnotGivvySetup-{rel.version}.exe"
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(rel.url, headers={"User-Agent": UA["User-Agent"]}), timeout=120) as r, \
            dest.open("wb") as f:
        total = int(r.headers.get("Content-Length") or rel.size or 0)
        done = 0
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk); h.update(chunk); done += len(chunk)
            progress(done, total)
    if h.hexdigest().lower() != rel.sha256:
        dest.unlink(missing_ok=True)
        raise ValueError("the download does not match the published checksum; it was deleted and nothing was installed")
    return dest


def install_and_restart(installer: Path) -> None:
    """Inno Setup closes the running app, replaces the program files (settings and
    history are not part of them) and starts it again."""
    # Started a few seconds late, through cmd, so that THIS program has exited and released its files
    # before the installer looks. The caller must quit the app straight after calling this.
    cmd = (f'timeout /t 4 /nobreak >nul & "{installer}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART '
           f'/CLOSEAPPLICATIONS /FORCECLOSEAPPLICATIONS /RELAUNCH=1')
    subprocess.Popen(["cmd", "/c", cmd],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                     close_fds=True)


@dataclass(frozen=True)
class Plan:
    button: str        # what the one button says
    label: str         # the line next to it
    action: str        # what a click does: "check" or "install"


def plan(rel, current: str, installed: bool) -> Plan:
    """What Settings shows after a check. One button, no dialog, no link to go and click."""
    if rel is None:
        return Plan("Check for updates", f"You have the latest version ({current}).", "check")
    if not installed:
        return Plan("Check for updates", f"{rel.version} is released. This copy runs from the source folder, which is "
                                         f"where the updates are made, so there is nothing to install here.", "check")
    first = (rel.notes or "").strip().splitlines()[0][:150] if (rel.notes or "").strip() else ""
    return Plan(f"Update now to {rel.version}", f"{rel.version} is available ({rel.size / 1e6:.0f} MB). {first}".strip(),
                "install")


def can_self_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"
