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
             value = giveaways per hour (as OBSERVED) / expected entrants at the draw (givvy/crowd.py)
           The owner's example: 20 viewers with a pack every 5 minutes (12/h / 20.4 = 0.59) beats 10 viewers
           with one every 11 minutes (5.5/h / 13.7 = 0.40). Measured on 550 visits: under 5 viewers only 23%
           of visits ever saw a giveaway, so 'fewest viewers' alone parks the bot in dead streams.
           Leaving a fallback stream for a pack-only one weighs value by each stream's pack share (pack_share).

3. ORDER   not just left for being dead  >  kind (pack_only, mixed, other)  >  value  >  fewer viewers.
"""
from __future__ import annotations

import statistics
import threading
from dataclasses import dataclass

from .crowd import final_entrants
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


def pack_share(info, kind: str) -> float:
    """The share of a stream's giveaways that are packs (or better). The start rate counts EVERY giveaway a stream
    runs, buyers and non-packs included, so its pack wins per hour = value_of(...).wins_per_hour x this. From the
    listed quantities (Listed.pack_share) when the directory worked them out, else from the kind: a mixed stream
    counts as half packs, one with no pack listed as none, anything else (legacy, not read yet) as all packs."""
    share = getattr(info, "pack_share", -1)
    if share is not None and share >= 0:
        return float(share)
    return {PACK_ONLY: 1.0, MIXED: 0.5, OTHER: 0.0}.get(kind, 1.0)


class ActivityBook:
    """How often each stream ACTUALLY starts a giveaway: from the background listeners and from what each
    emulator sees. Shared by all accounts."""
    WINDOW_S = 45 * 60         # older sightings say little about now
    SAME_S = 60                # two reports inside this are the same giveaway (listener + emulator)
    UNPROVEN_PER_H = 2.0       # never watched: a cautious guess, so proven streams win
    ONE_SIGHTING_GAP_S = 900   # seen once: assume a giveaway every 15 minutes until a second one says otherwise
    QUIET_PROOF_S = 600        # watched this long with nothing: that is evidence too

    def __init__(self, sink=None, quiet_max_per_h: float = 0.5, quiet_memory_s: float = 2700,
                 crowd_from_seen: bool = True):
        self._lock = threading.Lock()
        self._starts: dict[str, list[float]] = {}
        self._watched_since: dict[str, float] = {}
        self._quiet: dict[str, tuple[float, float]] = {}   # show_id -> (from, until) of a listening spell that ended
        self._entrants: dict[str, list[int]] = {}
        self.sink = sink           # (kind, show_id, value, t) -> None: the database, so a restart forgets nothing
        self.quiet_max_per_h = quiet_max_per_h      # config: scoring.quiet_max_per_h (see rate)
        self.quiet_memory_s = quiet_memory_s        # config: scoring.quiet_memory_s (see watching)
        self.crowd_from_seen = crowd_from_seen      # config: scoring.crowd_from_seen (see entrants)

    def load(self, rows) -> None:
        """Preload what an earlier run learned: (kind, show_id, value, t) rows, oldest first. Not written back."""
        with self._lock:
            for kind, show_id, value, t in rows:
                if kind == "start":
                    ts = self._starts.setdefault(show_id, [])
                    if not ts or t - ts[-1] >= self.SAME_S:
                        ts.append(t)
                        del ts[:-12]
                elif kind == "entrants":
                    xs = self._entrants.setdefault(show_id, [])
                    xs.append(int(value))
                    del xs[:-8]

    def _save(self, kind: str, show_id: str, value: float, t: float) -> None:
        if self.sink is not None:
            try:
                self.sink(kind, show_id, value, t)
            except Exception:
                pass

    # ---- what comes in ----
    def record(self, show_id: str, now: float) -> None:
        with self._lock:
            ts = self._starts.setdefault(show_id, [])
            if ts and now - ts[-1] < self.SAME_S:
                return
            ts.append(now)
            del ts[:-12]
            # Quiet now counts from this giveaway, not from when listening began.
            if show_id in self._watched_since:
                self._watched_since[show_id] = now
            self._quiet.pop(show_id, None)
        self._save("start", show_id, 0.0, now)

    def note_entrants(self, show_id: str, n: int | None) -> None:
        if n is None:
            return
        with self._lock:
            xs = self._entrants.setdefault(show_id, [])
            xs.append(int(n))
            del xs[:-8]
        import time as _t
        self._save("entrants", show_id, float(n), _t.time())

    def watching(self, show_ids, now: float) -> None:
        """Called with the streams the listeners are on right now.

        The 40 listener slots are re-aimed every minute. A stream that dropped out used to lose its quiet proof at
        once, fall back to the never-watched guess, climb back into the slots and look promising again (100 of the
        359 quiet leaves since 9/19 were revisited by the same account within 3 h). Now the quiet it was listened
        to is kept for quiet_memory_s and counts again when it comes back; the time away does not count as quiet
        (0 = forget at once, as in 1.1.x)."""
        ids = set(show_ids)
        with self._lock:
            for i in ids:
                if i not in self._watched_since:
                    old = self._quiet.pop(i, None)
                    self._watched_since[i] = (now - (old[1] - old[0]) if old and now - old[1] <= self.quiet_memory_s
                                              else now)
            for i in [i for i in self._watched_since if i not in ids]:
                since = self._watched_since.pop(i)
                if self.quiet_memory_s > 0:
                    self._quiet[i] = (since, now)

    def forget(self, live_ids) -> None:
        live = set(live_ids)
        with self._lock:
            for d in (self._starts, self._entrants, self._watched_since, self._quiet):
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
            q = self._quiet.get(show_id)
        if len(ts) >= 2:
            gap = max(60.0, statistics.median(b - a for a, b in zip(ts, ts[1:])))
            overdue = now - ts[-1]
            if overdue > 2 * gap:                      # it used to run them; it has stopped
                gap = overdue
            return 3600.0 / gap, True
        if len(ts) == 1:
            return 3600.0 / max(self.ONE_SIGHTING_GAP_S, now - ts[0]), True
        if since is not None:
            watched = now - since
        elif q and now - q[1] <= self.quiet_memory_s:
            watched = q[1] - q[0]                      # listened to earlier; out of the listener slots now
        else:
            watched = 0.0
        if watched >= self.QUIET_PROOF_S:
            # At most one in all that time, probably none. Quiet can only LOWER the guess. 0.5 x 3600 / 600 = 3.0/h
            # after ten silent minutes used to beat the 2.0/h guess for a stream never watched: a 1-viewer stream
            # silent 10 min scored 3.0 / 2.18 = 1.38 and beat a proven every-5-minutes 40-viewer one (1.30). Read
            # the 2.0/h guess as some streams running one every 319 s (the median gap over 273 stays, measured
            # 2026-09-24) and the rest none: an active one stays silent for 600 s with P = e^(-600/319) = 0.15,
            # so ten silent minutes leave about 0.36/h. quiet_max_per_h = 0.5 rounds that up; 3.0 or more gives
            # the old formula back.
            return min(self.quiet_max_per_h, 0.5 * 3600.0 / watched), True
        return self.UNPROVEN_PER_H, False

    def entrants(self, show_id: str, viewers: int) -> float:
        """Expected entrants at the draw (givvy/crowd.py). The counts our accounts read on cards in this stream
        refine it unless crowd_from_seen is off."""
        with self._lock:
            obs = list(self._entrants.get(show_id, ()))
        return final_entrants(viewers, obs if self.crowd_from_seen else None)

    def observed(self, show_id: str) -> list[int]:
        """The counts our accounts read on cards in this stream (the last 8 first reads), for the crowd hop's decision
        log (seen_median)."""
        with self._lock:
            return list(self._entrants.get(show_id, ()))


@dataclass(frozen=True)
class Value:
    wins_per_hour: float
    per_hour: float
    entrants: float
    proven: bool

    def text(self) -> str:
        return (f"{self.per_hour:.1f} giveaways/h{'' if self.proven else ' (guess)'} / ~{self.entrants:.0f} at the draw"
                f" = {self.wins_per_hour:.3f}")


def value_of(show, book: ActivityBook | None, now: float, runs_seen: int = 0) -> Value:
    if book is None:                                   # callers without a book (older tests): a rough stand-in
        per_hour, proven = (ActivityBook.UNPROVEN_PER_H + 2.0 * min(runs_seen, 5)), runs_seen > 0
        entrants = final_entrants(show.viewers)
    else:
        per_hour, proven = book.rate(show.id, now)
        entrants = book.entrants(show.id, show.viewers)
    return Value(per_hour / entrants, per_hour, entrants, proven)
