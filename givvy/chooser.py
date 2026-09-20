"""Which stream is worth sitting in. One coherent rule set, agreed with the owner on 2026-09-19 after a day
of patches that fought each other.

THE GOAL   win as many PACK-OR-BETTER giveaways as possible.

1. KIND    Every stream is classified from the giveaways it lists (title AND description):
             pack_only    every open giveaway is a pack or better (pack, sealed product, slab)   <- where we want to be
             mixed        packs, and other things too                                           <- fallback
             other        no pack, but something that is not junk                               <- last resort
             junk         only cards, stickers, keychains, butter...                            never
             buyers_only  nothing anyone can enter without buying                               never
             nothing      lists no giveaway                                                     never
             unknown      its list has not been read yet                                        not yet
           A buyers giveaway next to open ones does not change the kind: it is simply skipped.

2. VALUE   Within a kind, streams are ordered by EXPECTED WINS PER HOUR, not by viewers alone:
             value = giveaways per hour (as OBSERVED) / expected number of entrants
           The owner's example: 20 viewers with a pack every 5 minutes (12/h / 5.6 = 2.1) beats 10 viewers
           with one every 11 minutes (5.5/h / 3.8 = 1.4). Measured on 550 visits: under 5 viewers only 23%
           of visits ever saw a giveaway, so 'fewest viewers' alone parks the bot in dead streams.

3. ORDER   not just left for being dead  >  kind (pack_only, mixed, other)  >  value  >  fewer viewers.
"""
from __future__ import annotations

import statistics
import threading
from dataclasses import dataclass

from .giveaway_info import grade_listing, is_buyers_only

PACK_ONLY, MIXED, OTHER, JUNK, BUYERS_ONLY, NOTHING, UNKNOWN, LEGACY = (
    "pack_only", "mixed", "other", "junk", "buyers_only", "nothing", "unknown", "legacy")
KIND_RANK = {PACK_ONLY: 3, MIXED: 2, OTHER: 1, LEGACY: 1}           # anything else is never joined
WHY_NOT = {JUNK: "junk giveaways only", BUYERS_ONLY: "buyers only", NOTHING: "nothing listed",
           UNKNOWN: "giveaways not read yet"}
LABEL = {PACK_ONLY: "PACKS ONLY", MIXED: "packs + other", OTHER: "no pack", LEGACY: ""}


def classify(listings) -> str:
    """`listings` None = not looked up yet."""
    if listings is None:
        return UNKNOWN
    if not listings:
        return NOTHING
    grades = [grade_listing(g) for g in listings if not is_buyers_only(g)]
    if not grades:
        return BUYERS_ONLY
    if all(g == 2 for g in grades):
        return PACK_ONLY
    if 2 in grades:
        return MIXED
    return OTHER if 1 in grades else JUNK


def estimate_entrants(viewers: int, observed: list[int] | None = None) -> float:
    """Measured medians: 3 entrants under 10 viewers, 6 at 10-40, 10 at 40-100, 56 at 100+  ->  2 + 0.18 * viewers.
    Counts the bot read itself are taken early in a giveaway, so they only ever RAISE the estimate."""
    model = 2.0 + 0.18 * max(0, viewers)
    if observed:
        return max(model, statistics.median(observed) + 1.0)
    return model


class ActivityBook:
    """How often each stream ACTUALLY starts a giveaway: from the background listeners and from what each
    emulator sees. Shared by all accounts."""
    WINDOW_S = 45 * 60         # older sightings say little about now
    SAME_S = 60                # two reports inside this are the same giveaway (listener + emulator)
    UNPROVEN_PER_H = 2.0       # never watched: a cautious guess, so proven streams win
    ONE_SIGHTING_GAP_S = 900   # seen once: assume a giveaway every 15 minutes until a second one says otherwise
    QUIET_PROOF_S = 600        # watched this long with nothing: that is evidence too

    def __init__(self):
        self._lock = threading.Lock()
        self._starts: dict[str, list[float]] = {}
        self._watched_since: dict[str, float] = {}
        self._entrants: dict[str, list[int]] = {}

    # ---- what comes in ----
    def record(self, show_id: str, now: float) -> None:
        with self._lock:
            ts = self._starts.setdefault(show_id, [])
            if ts and now - ts[-1] < self.SAME_S:
                return
            ts.append(now)
            del ts[:-12]

    def note_entrants(self, show_id: str, n: int | None) -> None:
        if n is None:
            return
        with self._lock:
            xs = self._entrants.setdefault(show_id, [])
            xs.append(int(n))
            del xs[:-8]

    def watching(self, show_ids, now: float) -> None:
        """Called with the streams the listeners are on right now."""
        ids = set(show_ids)
        with self._lock:
            for i in ids:
                self._watched_since.setdefault(i, now)
            for i in [i for i in self._watched_since if i not in ids]:
                del self._watched_since[i]

    def forget(self, live_ids) -> None:
        live = set(live_ids)
        with self._lock:
            for d in (self._starts, self._entrants, self._watched_since):
                for i in [i for i in d if i not in live]:
                    del d[i]

    # ---- what comes out ----
    def interval(self, show_id: str, now: float) -> float | None:
        """Median seconds between this stream's giveaways; None until two have been seen."""
        with self._lock:
            ts = [t for t in self._starts.get(show_id, ()) if now - t <= self.WINDOW_S]
        if len(ts) < 2:
            return None
        return max(60.0, statistics.median(b - a for a, b in zip(ts, ts[1:])))

    def last_start(self, show_id: str, now: float) -> float | None:
        """When this stream last started a giveaway (as seen by anyone), or None if that was too long ago."""
        with self._lock:
            ts = [t for t in self._starts.get(show_id, ()) if now - t <= self.WINDOW_S]
        return ts[-1] if ts else None

    def rate(self, show_id: str, now: float) -> tuple[float, bool]:
        """(giveaways per hour, proven). 'Proven' covers proven QUIET as well."""
        with self._lock:
            ts = [t for t in self._starts.get(show_id, ()) if now - t <= self.WINDOW_S]
            since = self._watched_since.get(show_id)
        if len(ts) >= 2:
            gap = max(60.0, statistics.median(b - a for a, b in zip(ts, ts[1:])))
            overdue = now - ts[-1]
            if overdue > 2 * gap:                      # it used to run them; it has stopped
                gap = overdue
            return 3600.0 / gap, True
        if len(ts) == 1:
            return 3600.0 / max(self.ONE_SIGHTING_GAP_S, now - ts[0]), True
        if since is not None and now - since >= self.QUIET_PROOF_S:
            return 0.5 * 3600.0 / (now - since), True  # at most one in all that time, probably none
        return self.UNPROVEN_PER_H, False

    def entrants(self, show_id: str, viewers: int) -> float:
        with self._lock:
            obs = list(self._entrants.get(show_id, ()))
        return estimate_entrants(viewers, obs)


@dataclass(frozen=True)
class Value:
    wins_per_hour: float
    per_hour: float
    entrants: float
    proven: bool

    def text(self) -> str:
        return (f"{self.per_hour:.1f} giveaways/h{'' if self.proven else ' (guess)'} / ~{self.entrants:.0f} entrants"
                f" = {self.wins_per_hour:.2f}")


def value_of(show, book: ActivityBook | None, now: float, runs_seen: int = 0) -> Value:
    if book is None:                                   # callers without a book (older tests): a rough stand-in
        per_hour, proven = (ActivityBook.UNPROVEN_PER_H + 2.0 * min(runs_seen, 5)), runs_seen > 0
        entrants = estimate_entrants(show.viewers)
    else:
        per_hour, proven = book.rate(show.id, now)
        entrants = book.entrants(show.id, show.viewers)
    return Value(per_hour / entrants, per_hour, entrants, proven)
