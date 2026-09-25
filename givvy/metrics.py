"""How each account is actually doing, from the database: so the bot can be tuned from numbers instead of from
whatever happened to be on screen when someone looked.

    entered      the app said "You're in" (or the badge showed the tick)
    unconfirmed  Enter was tapped and never proven either way
    skipped      seen and deliberately not entered (buyers, junk)
    missed       a giveaway that ran while we were there and was not entered. Two sources: the ones the bot knew it
                 missed ('could not open the card in time'), and GAPS IN THE NUMBERING - sellers number their
                 giveaways (#11, #12, ...), so #11 then #13 in one stream means #12 went by without us.
    wins         the winner dialog named this account
"""
from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field

NUMBERED = re.compile(r"^(.*?)\s*#\s*(\d+)\s*$")
GAP_WINDOW_S = 30 * 60       # two numbered giveaways further apart than this: we may have left in between
MAX_GAP = 5                  # a bigger jump is a different series, or we were away, not a run of misses


@dataclass
class Stats:
    entered: int = 0
    unconfirmed: int = 0
    skipped: int = 0
    missed_known: int = 0
    missed_numbering: int = 0
    wins: int = 0
    entrants: list = field(default_factory=list)

    @property
    def missed(self) -> int:
        return self.missed_known + self.missed_numbering

    @property
    def median_entrants(self) -> int | None:
        return int(statistics.median(self.entrants)) if self.entrants else None

    @property
    def caught(self) -> float | None:
        """Of the giveaways we wanted, the share we got into."""
        want = self.entered + self.unconfirmed + self.missed
        return (self.entered + self.unconfirmed) / want if want else None


def numbered(prize: str) -> tuple[str, int] | None:
    """'FREE PACK #12' -> ('free pack', 12). The card appends a running number; the listing does not."""
    m = NUMBERED.match((prize or "").split(" [")[0].strip())
    return (m.group(1).strip().lower(), int(m.group(2))) if m else None


def report(store, since: float, until: float | None = None) -> dict[str, Stats]:
    until = until or time.time()
    out: dict[str, Stats] = {}
    rows = store._x("SELECT account, show_id, result, entries_seen, entered_at, COALESCE(prize,'') FROM entries "
                    "WHERE entered_at>=? AND entered_at<? ORDER BY entered_at", (since, until)).fetchall()
    series: dict[tuple, list] = {}
    for account, show_id, result, n, t, prize in rows:
        s = out.setdefault(account, Stats())
        if result == "entered":
            s.entered += 1
        elif result == "unverified":
            s.unconfirmed += 1
        elif result == "skipped":
            s.skipped += 1
        elif result == "missed":
            s.missed_known += 1
        if result in ("entered", "unverified") and n is not None:
            s.entrants.append(n)
        num = numbered(prize)
        if num:
            series.setdefault((account, show_id, num[0]), []).append((t, num[1]))
    for (account, _show, _base), seen in series.items():
        for (t1, n1), (t2, n2) in zip(seen, seen[1:]):
            gap = n2 - n1 - 1
            if 0 < gap <= MAX_GAP and t2 - t1 <= GAP_WINDOW_S:
                out[account].missed_numbering += gap
    for (account,) in store._x("SELECT COALESCE(account,'') FROM wins WHERE detected_at>=? AND detected_at<?",
                               (since, until)).fetchall():
        out.setdefault(account or "(unknown)", Stats()).wins += 1
    return out


def format_report(stats: dict[str, Stats], title: str) -> str:
    lines = [title, f"{'account':14s} {'entered':>7s} {'unconf.':>7s} {'skipped':>7s} {'missed':>6s} {'caught':>6s} "
                    f"{'wins':>4s} {'median entrants':>15s}"]
    for account in sorted(stats):
        s = stats[account]
        caught = f"{s.caught:.0%}" if s.caught is not None else "-"
        med = str(s.median_entrants) if s.median_entrants is not None else "-"
        lines.append(f"{account:14s} {s.entered:7d} {s.unconfirmed:7d} {s.skipped:7d} {s.missed:6d} {caught:>6s} "
                     f"{s.wins:4d} {med:>15s}")
    return "\n".join(lines)
