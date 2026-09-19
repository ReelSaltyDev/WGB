"""Which build is this? The installer build stamps givvy/_build.py with the git
commit and date, so 'is my friend on the latest version?' has an answer."""
from __future__ import annotations

import subprocess
from pathlib import Path


__version__ = "1.0.7"          # bumped by installer/release.py; the release tag is 'v' + this
UPDATE_REPO = "ReelSaltyDev/WGB"               # "owner/name" on GitHub; empty = the update button says updates are not set up


def short_version() -> str:
    """For the corner of the main window: 'v1.0.5', or 'v1.0.5 + source abc1234' when run from the source
    folder (the number is then only the LAST release; the code may be newer)."""
    info = build_info()
    return f"v{__version__}" if info.startswith("version") else f"v{__version__} + {info}"


def build_info() -> str:
    try:
        from ._build import COMMIT, DATE, DIRTY
        return f"version {__version__}  (build {COMMIT}{'+changes' if DIRTY else ''}, {DATE})"
    except Exception:
        pass
    try:                                              # running from the source folder
        root = Path(__file__).resolve().parent.parent
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                                timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        return f"source {commit or '?'}"
    except Exception:
        return "source"
