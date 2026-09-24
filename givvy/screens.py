"""Cleans up the screenshots folder, which nothing emptied before.

Measured 2026-09-24: 94 screenshots, 177 MB, growing ~35 MB a day; 162 MB of it win screenshots (~2.5 MB each, a
full-resolution PNG). Discord already has its own copy of each one, sent with the alert.

Only the bot's own files are touched: '<unix time>_<what>.png' (and .xml) as camper._shot names them. Anything else in
the folder was put there by hand and is left alone."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

OURS = re.compile(r"^(\d{9,11})_[\w.-]+\.(png|xml)$")
EVERY_S = 6 * 3600


def ours(folder: Path) -> list[tuple[float, int, Path]]:
    """(time taken, size, path) for the bot's own screenshots, oldest first."""
    found = []
    for p in Path(folder).iterdir() if Path(folder).is_dir() else ():
        m = OURS.match(p.name)
        if m and p.is_file():
            try:
                found.append((float(m.group(1)), p.stat().st_size, p))
            except OSError:
                pass
    return sorted(found)


def prune(folder: Path | str, keep_days: float = 7, max_mb: float = 300, now: float | None = None) -> tuple[int, int]:
    """Delete the bot's screenshots older than keep_days, then the oldest until the rest fit in max_mb.
    0 turns either limit off. Returns (files deleted, bytes freed)."""
    now = time.time() if now is None else now
    files = ours(Path(folder))
    doomed = [f for f in files if keep_days and f[0] < now - keep_days * 86400]
    left = [f for f in files if f not in doomed]
    total = sum(size for _t, size, _p in left)
    if max_mb:
        for f in left:                                   # oldest first
            if total <= max_mb * 1_000_000:
                break
            doomed.append(f)
            total -= f[1]
    deleted = freed = 0
    for _t, size, p in doomed:
        try:
            p.unlink()
            deleted += 1
            freed += size
        except OSError:                                   # open in a viewer, say: next time
            pass
    return deleted, freed


def keep_tidy(folder: Path, keep_days: float, max_mb: float, every_s: float = EVERY_S, sleep=time.sleep):
    """Background loop: now, then every 6 hours."""
    while True:
        try:
            n, freed = prune(folder, keep_days, max_mb)
            if n:
                log.info("screenshots: deleted %d old ones (%.0f MB) from %s", n, freed / 1e6, folder)
        except Exception:
            log.exception("screenshot cleanup")
        sleep(every_s)
