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
import time
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


def _resolve(url: str, timeout: int = 30) -> str:
    """Where an https URL ends up after its redirects."""
    if not url.startswith("https://"):
        raise ValueError("refusing a non-HTTPS update URL")
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]}, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl()


def _latest_without_the_api(repo: str, fetch, resolve) -> Release | None:
    """GitHub's API allows 60 anonymous requests an hour PER ADDRESS; behind a VPN or a shared connection that
    is often already spent and it answers 403. The ordinary web addresses are not limited that way:
    /releases/latest redirects to the newest tag, and the installer and its checksum sit at fixed names under it."""
    final = resolve(f"https://github.com/{repo}/releases/latest")
    m = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/releases/tag/(v?\d+(?:\.\d+)+)", final or "", re.I)
    if not m:
        return None                                    # somewhere we did not ask to go: no update
    tag = m.group(1)
    base = f"https://github.com/{repo}/releases/download/{tag}/"      # pinned to that tag, not to 'latest'
    sha = fetch(base + ASSET + ".sha256").decode("ascii", "replace").split()
    sha = sha[0].lower() if sha else ""
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None                                    # no checksum, no update: never run an unverified download
    return Release(version=tag, notes="", url=base + ASSET, sha256=sha, size=0,
                   page=f"https://github.com/{repo}/releases/tag/{tag}")


def latest(repo: str, fetch=_get, resolve=_resolve) -> Release | None:
    """The newest published release of owner/name, or None if there is none usable."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        return None
    try:
        raw = fetch(f"https://api.github.com/repos/{repo}/releases/latest")
    except Exception as api_error:
        log.info("GitHub's API would not answer (%s); using the web addresses instead", api_error)
        try:
            return _latest_without_the_api(repo, fetch, resolve)
        except Exception:
            raise api_error                            # report the first failure: it is the informative one
    data = json.loads(raw)
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


def newest_version(repo: str, fetch=_get, resolve=_resolve) -> str:
    """The tag of the newest release, whether or not it is newer than ours. '' when there is none."""
    rel = latest(repo, fetch, resolve)
    return rel.version if rel else ""


def check(repo: str, current: str, fetch=_get, resolve=_resolve) -> Release | None:
    """A release newer than `current`, else None."""
    rel = latest(repo, fetch, resolve)
    return rel if rel and is_newer(rel.version, current) else None


def explain(error: Exception) -> str:
    """Why a check failed, in words a person can act on (it used to be the raw Python error)."""
    code = getattr(error, "code", None)
    if code in (403, 429):
        return ("GitHub refused: too many requests from your internet address in the last hour (common on a VPN "
                "or a shared connection). Try again later, or download the installer from the releases page.")
    if code == 404:
        return "GitHub has no release for this program yet."
    if isinstance(error, (urllib.error.URLError, OSError, TimeoutError)):
        return f"Could not reach GitHub ({getattr(error, 'reason', error)}). Check the internet connection or a firewall."
    return f"Could not check GitHub for updates: {error}"


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


PENDING = "update_pending.json"       # beside the program: which update was started, and where its log is


def setup_command(installer: Path, log_path: Path, relaunch: bool = True) -> list[str]:
    """Setup's own command line, as a LIST: no cmd.exe in between.

    It used to be `cmd /c "timeout /t 4 & "<setup>" /VERYSILENT ..."`. Measured 2026-09-23, from a windowless
    process like the app: Python turns the quotes around the setup path into \", which cmd.exe does not
    understand, so NOTHING ran - a friend saw the app close and nothing happen. And `timeout` is not dependable
    there either (on one PC it was a different program called timeout, which failed at once). Setup now starts
    directly; Restart Manager closes the app if it has not finished quitting (CloseApplications=force)."""
    # /SILENT, not /VERYSILENT: no questions, but its progress window shows, so the update is SEEN happening.
    # Measured: a brand-new (just downloaded) installer took ~2 minutes to start the first time - Windows checking
    # the unsigned file - then 0.3 s on later runs. With nothing on screen that looks like 'nothing happened'.
    args = [str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
            "/FORCECLOSEAPPLICATIONS", f"/LOG={log_path}"]
    if relaunch:
        args.append("/RELAUNCH=1")
    return args


def install_and_restart(installer: Path, version: str = "", here: Path | None = None, relaunch: bool = True,
                        popen=None) -> Path:
    """Start Setup on its own and remember what was started, so the next run can say whether it worked.
    Returns the path of Setup's log. The caller must quit the app straight after calling this."""
    here = Path(here or (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()))
    log_path = Path(tempfile.gettempdir()) / "WhatnotGivvy-update.log"
    try:
        (here / PENDING).write_text(json.dumps({"to": version, "log": str(log_path), "at": time.time()}),
                                    encoding="utf-8")
    except OSError:
        pass
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    (popen or subprocess.Popen)(setup_command(installer, log_path, relaunch), creationflags=flags, close_fds=True)
    return log_path


def update_outcome(current: str, here: Path) -> str | None:
    """On start: did the update started last time actually install? Says so once, then forgets it."""
    p = Path(here) / PENDING
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    try:
        p.unlink()
    except OSError:
        pass
    want = str(info.get("to", "")).lstrip("vV")
    if not want:
        return None
    if parse_version(want) == parse_version(current):
        return f"Updated to {current}."
    return (f"The update to {want} did not install (still {current}). Setup's log: {info.get('log', '?')}. "
            f"Try Settings > Check for updates again, or run WhatnotGivvySetup.exe from the releases page.")


@dataclass(frozen=True)
class Plan:
    button: str        # what the one button says
    label: str         # the line next to it
    action: str        # what a click does: "check" or "install"


def plan(rel, current: str, installed: bool, newest: str = "") -> Plan:
    """What Settings shows after a check. One button, no dialog, no link to go and click.
    `newest` is the version GitHub reported, so 'up to date' proves the check got through: a friend could not
    tell 'you already have it' from 'the check did nothing'."""
    if rel is None:
        import time
        seen = f" GitHub's newest is {newest}." if newest else ""
        return Plan("Check for updates", f"Up to date: you have {current}.{seen} Checked {time.strftime('%H:%M')}.", "check")
    if not installed:
        return Plan("Check for updates", f"{rel.version} is released. This copy runs from the source folder, which is "
                                         f"where the updates are made, so there is nothing to install here.", "check")
    first = (rel.notes or "").strip().splitlines()[0][:150] if (rel.notes or "").strip() else ""
    size = f" ({rel.size / 1e6:.0f} MB)" if rel.size else ""          # unknown when GitHub's API would not answer
    return Plan(f"Update now to {rel.version}", f"{rel.version} is available{size}. {first}".strip(), "install")


def diagnose(out_path: Path, current: str | None = None) -> int:
    """`WhatnotGivvy.exe --check-update`: do exactly what Settings > Check for updates does, from inside the
    PACKAGED program, and write what happened to a file (a windowed exe has no console). For support: a friend
    reported that the button never offered the update, and the check had only ever been run from source."""
    import traceback
    from .version import UPDATE_REPO, __version__, build_info
    current = current or os.environ.get("GIVVY_PRETEND_VERSION") or __version__
    lines = [f"build: {build_info()}", f"comparing against version: {current}", f"repo: {UPDATE_REPO}",
             f"packaged (frozen): {bool(getattr(sys, 'frozen', False))}", f"can install by itself: {can_self_update()}"]
    code = 0
    try:
        rel = check(UPDATE_REPO, current)
        p = plan(rel, current, can_self_update(), newest_version(UPDATE_REPO) if rel is None else rel.version)
        lines += [f"newest release seen: {rel.version if rel else '(none newer)'}",
                  f"button would read: {p.button}", f"line beside it: {p.label}", f"a click would: {p.action}"]
        if rel is not None:
            lines.append(f"installer: {rel.url}  ({rel.size} bytes, sha256 {rel.sha256[:16]}...)")
    except Exception:
        code = 1
        lines += ["THE CHECK FAILED:", explain(sys.exc_info()[1]), traceback.format_exc()]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return code


def can_self_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"
