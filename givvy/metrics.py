"""How each account is actually doing, from the database: so the bot can be tuned from numbers instead of from
whatever happened to be on screen when someone looked.

    entered      the app said "You're in" (or the badge showed the tick)
    unconfirmed  Enter was tapped and never proven either way
    skipped      seen and deliberately not entered (buyers, junk)
    missed       a giveaway that ran while we were there and was not entered. Two sources: the ones the bot knew it
                 missed ('could not open the card in time'), and GAPS IN THE NUMBERING - sellers number their
                 giveaways (#11, #12, ...), so #11 then #13 in one stream means #12 went by without us.
    wins         from win_history: the winner dialog (deduplicated) and the Givvy Wins tracker. Not from the legacy
                 wins table, whose rows are mostly repeats of one win and have no account.

Everything above is counted per GIVEAWAY (giveaways()), not per row: after the 9/23 rebuild 13-19% of 1.1.x's entry
rows repeated a giveaway it had already recorded (the badge flickered, and it was tapped and recorded again); 1.2.0
writes one row. The same rule for every period keeps the two comparable.
"""
from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field

from .giveaway_info import says_buyers
from .store import same_prize

NUMBERED = re.compile(r"^(.*?)\s*#\s*(\d+)\s*$")
GAP_WINDOW_S = 30 * 60       # two numbered giveaways further apart than this: we may have left in between
MAX_GAP = 5                  # a bigger jump is a different series, or we were away, not a run of misses
SAME_TITLE_S = 420           # the same unnumbered title again within hold_timeout_s: the same giveaway
NO_TITLE_S = 180             # a row with no title: on 9/23-24 repeats followed 18-144 s after the first row, and the
                             # next real giveaway at least 308 s after
RANK = {"entered": 3, "unverified": 2, "skipped": 1, "missed": 0}     # a giveaway's result: the best of its rows


@dataclass
class Stats:
    entered: int = 0
    unconfirmed: int = 0
    skipped: int = 0
    missed_known: int = 0
    missed_numbering: int = 0
    wins: int = 0
    entrants: list = field(default_factory=list)
    entry_rows: int = 0          # raw 'entered' + 'unverified' rows, repeats included

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

    @property
    def rows_per_entry(self) -> float | None:
        """Entry rows per giveaway entered: about 1.2 for 1.1.x after the rebuild, 1.0 when each is recorded once."""
        n = self.entered + self.unconfirmed
        return self.entry_rows / n if n else None


def numbered(prize: str) -> tuple[str, int] | None:
    """'FREE PACK #12' -> ('free pack', 12). The card appends a running number; the listing does not."""
    m = NUMBERED.match((prize or "").split(" [")[0].strip())
    return (m.group(1).strip().lower(), int(m.group(2))) if m else None


def count_collapsed(top: int | None, n: int | None) -> bool:
    """A new giveaway behind the same badge: the entry count fell by at least 3 AND to at most half of the highest
    seen. Inside one giveaway it also dips by one or two (26 -> 25, 23 -> 21: eleven false alarms in ten minutes,
    measured live the day the rule shipped). One rule for the camper, the report and the crowd hop."""
    return top is not None and n is not None and top - n >= 3 and n * 2 <= top


@dataclass
class Giveaway:
    """One giveaway one account saw in one stream, from one or more entries rows."""
    account: str
    show_id: str
    at: float                    # its first row
    result: str                  # the best of its rows (RANK); a 'missed' buyers card counts as 'skipped'
    entries_seen: int | None     # the first count found: the count at entry, which givvy/crowd.py is calibrated on
    prize: str                   # the first title found
    rows: int = 1
    top: int | None = None       # the highest count on any of its rows
    last: float = 0.0            # its last row


def _joins(g: Giveaway, t: float, n: int | None, prize: str) -> bool:
    a, b = numbered(g.prize), numbered(prize)
    if a and b:
        return a == b and t - g.last <= GAP_WINDOW_S
    if g.prize and prize:
        return same_prize(g.prize, prize) and t - g.last <= SAME_TITLE_S and not count_collapsed(g.top, n)
    return t - g.last <= NO_TITLE_S and not count_collapsed(g.top, n)


def giveaways(rows) -> list[Giveaway]:
    """Which entries rows are the same giveaway. rows = (account, show_id, result, entries_seen, entered_at, prize),
    oldest first. A row joins the giveaway last seen for that account in that stream when both titles carry the same
    #N (within GAP_WINDOW_S); when a title has no number, the same title within SAME_TITLE_S; when a title is
    missing, within NO_TITLE_S; the last two only if the count has not collapsed. Numbered neighbours stay apart
    however close (#7 and #8 came 23 s apart). The 1.1.x baseline holds the repeat rows and the 1.2.0 trial will not:
    counted per row, the trial would show ~17% fewer entries per hour than the baseline for nothing."""
    out: list[Giveaway] = []
    open_: dict[tuple, Giveaway] = {}
    for account, show_id, result, n, t, prize in rows:
        prize = prize or ""
        g = open_.get((account, show_id))
        if g is not None and _joins(g, t, n, prize):
            g.rows += 1
            g.last = t
            g.top = max((x for x in (g.top, n) if x is not None), default=None)
            g.prize = g.prize or prize
            if g.entries_seen is None:
                g.entries_seen = n
            if RANK.get(result, -1) > RANK.get(g.result, -1):
                g.result = result
            continue
        g = Giveaway(account, show_id, t, result, n, prize, top=n, last=t)
        open_[(account, show_id)] = g
        out.append(g)
    for g in out:
        # 1.1.x recorded a buyers card with no Enter button as 'missed', four reads at a time, after every flicker
        # ('BUYERS GIVY RANDOM BOOSTER PACK #3': 10 rows, 9/24 06:36-06:41). It was never ours to enter.
        if g.result == "missed" and says_buyers(g.prize):
            g.result = "skipped"
    return out


def report(store, since: float, until: float | None = None) -> dict[str, Stats]:
    until = until or time.time()
    out: dict[str, Stats] = {}
    rows = store._x("SELECT account, show_id, result, entries_seen, entered_at, COALESCE(prize,'') FROM entries "
                    "WHERE entered_at>=? AND entered_at<? ORDER BY entered_at", (since, until)).fetchall()
    for account, _show, result, _n, _t, _prize in rows:
        s = out.setdefault(account, Stats())
        if result in ("entered", "unverified"):
            s.entry_rows += 1
    series: dict[tuple, list] = {}
    for g in giveaways(rows):
        s = out.setdefault(g.account, Stats())
        if g.result == "entered":
            s.entered += 1
        elif g.result == "unverified":
            s.unconfirmed += 1
        elif g.result == "skipped":
            s.skipped += 1
        elif g.result == "missed":
            s.missed_known += 1
        if g.result in ("entered", "unverified") and g.entries_seen is not None:
            s.entrants.append(g.entries_seen)
        num = numbered(g.prize)
        if num:
            series.setdefault((g.account, g.show_id, num[0]), []).append((g.at, num[1]))
    for (account, _show, _base), seen in series.items():
        for (t1, n1), (t2, n2) in zip(seen, seen[1:]):
            gap = n2 - n1 - 1
            if 0 < gap <= MAX_GAP and t2 - t1 <= GAP_WINDOW_S:
                out[account].missed_numbering += gap
    for (account,) in store._x("SELECT COALESCE(account,'') FROM win_history WHERE at>=? AND at<?",
                               (since, until)).fetchall():
        out.setdefault(account or "(unknown)", Stats()).wins += 1
    return out


def format_report(stats: dict[str, Stats], title: str) -> str:
    lines = [title, f"{'account':14s} {'entered':>7s} {'unconf.':>7s} {'skipped':>7s} {'missed':>6s} {'caught':>6s} "
                    f"{'wins':>4s} {'median entrants':>15s} {'rows/entry':>10s}"]
    for account in sorted(stats):
        s = stats[account]
        caught = f"{s.caught:.0%}" if s.caught is not None else "-"
        med = str(s.median_entrants) if s.median_entrants is not None else "-"
        rpe = f"{s.rows_per_entry:.2f}" if s.rows_per_entry is not None else "-"
        lines.append(f"{account:14s} {s.entered:7d} {s.unconfirmed:7d} {s.skipped:7d} {s.missed:6d} {caught:>6s} "
                     f"{s.wins:4d} {med:>15s} {rpe:>10s}")
    return "\n".join(lines)
