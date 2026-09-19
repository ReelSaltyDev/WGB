"""What each live stream is giving away, for the Live streams list.

Whatnot's web API will not say which giveaway is RUNNING (different id namespace,
measured early on), but it does list the giveaway items a seller has queued, with
how many of each are left. That is what is shown. When one of our accounts has
read a real prize title off the screen in that stream, that leads the text.

One stream is one HTTP request (~0.4s), so they are fetched a few at a time in
the background, most interesting streams first, and re-fetched only when stale.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
import time

log = logging.getLogger(__name__)
SHOWN = 3
# "Buyers" giveaways cannot be entered without a purchase. Whatnot has a flag for it, but sellers also
# just write it in the title ("FREE ETB (BUYERS)"), so the words count as much as the flag.
BUYERS = re.compile(r"\bbuyer'?s?\b|\bpurchas|\bbought\b", re.I)
PACK = re.compile(r"\bpacks?\b", re.I)


@dataclass(frozen=True)
class Listed:
    """What one stream has queued, as the scanner needs it."""
    open: int          # giveaways anyone may enter
    pack: bool         # one of THOSE has 'pack' in its description
    buyers: int        # buyers-only giveaways (never entered)
    grade: int = field(default=-1, compare=False)    # best open giveaway: 2 pack, 1 something else, 0 mentions card(s)/sticker(s); -1 = from `pack`


# ...unless the words say the opposite: "Givvy no purchase necessary" (seen live), "non-buyers giveaway".
NOT_BUYERS = re.compile(r"\bno\s+purchase|\bnon[\s-]*buyer|\bnot\s+(?:a\s+)?buyer|\bno\s+buy", re.I)


def _norm_title(t: str) -> str:
    return re.sub(r"\s*#\s*\d+\s*$", "", t or "").strip().lower()      # the card appends the running number


def says_buyers(text: str) -> bool:
    text = text or ""
    return BUYERS.search(text) is not None and NOT_BUYERS.search(text) is None


# The owner: 'random Card, Pack, or Stickers' draws are mostly cards and stickers. Chosen last, still entered.
LESSER = re.compile(r"\bcards?\b|\bstickers?\b", re.I)


def grade_title(title: str) -> int:
    """2 = a pack and nothing lesser in the description, 1 = anything else, 0 = mentions card(s) or sticker(s)."""
    title = title or ""
    if LESSER.search(title):
        return 0
    return 2 if PACK.search(title) else 1


def is_buyers_only(g) -> bool:
    if NOT_BUYERS.search(g.title or ""):
        return False                       # the seller's own words beat a flag they may have left ticked
    return bool(g.buyer_appreciation) or says_buyers(g.title)


def describe(listings, seen: str = "", limit: int | None = SHOWN) -> str:
    """One line for the list. `listings` None = not fetched yet."""
    parts = []
    if seen:
        parts.append(f"now: {seen}")
    if listings is None:
        return parts[0] if parts else ""
    if not listings:
        return "  |  ".join(parts + ["none listed"]) if parts else "none listed"
    items = []
    shown = listings if limit is None else listings[:limit]
    for g in shown:
        t = g.title or "(untitled)"
        if g.quantity and g.quantity > 1:
            t += f" ×{g.quantity}"
        if is_buyers_only(g):
            t += " [buyers: skipped]"                    # cannot be entered without a purchase; worth knowing
        items.append(t)
    text = "  ·  ".join(items)
    if len(listings) > len(shown):
        text += f"  ·  +{len(listings) - len(shown)} more"
    return "  |  ".join(parts + [text])


class GiveawayDirectory:
    def __init__(self, client, per_round: int = 12, ttl_s: float = 180.0, cold_ttl_s: float = 600.0,
                 hot_n: int = 120, workers: int = 4):
        """Two tiers: the first `hot_n` streams by interest are re-fetched every
        `ttl_s` (their lists change and the scanner is choosing among them), the
        rest every `cold_ttl_s`. `workers` requests run at once: the 0.4s each is
        network wait, so 4 in flight covers ~600 streams in a couple of minutes at
        a steady 1-2 requests a second."""
        self.client = client
        self.per_round = per_round
        self.ttl_s = ttl_s
        self.cold_ttl_s = cold_ttl_s
        self.hot_n = hot_n
        self.workers = max(1, workers)
        self.version = 0                             # bumps whenever the text of any stream may have changed
        self._lock = threading.Lock()
        self._listings: dict[str, list] = {}
        self._tried_at: dict[str, float] = {}
        self._seen: dict[str, str] = {}

    def refresh(self, show_ids_by_interest: list[str], now: float | None = None) -> int:
        """Fetch up to `per_round` streams that are missing or stale. Call it every
        few seconds from a background thread; it is the rate limit."""
        now = time.time() if now is None else now
        live = set(show_ids_by_interest)
        with self._lock:
            for dead in [s for s in self._tried_at if s not in live]:
                self._tried_at.pop(dead, None); self._listings.pop(dead, None); self._seen.pop(dead, None)
            rank = {s: i for i, s in enumerate(show_ids_by_interest)}
            due = [s for s in show_ids_by_interest
                   if now - self._tried_at.get(s, -1e18) >= (self.ttl_s if rank[s] < self.hot_n else self.cold_ttl_s)]
            # never fetched first (in list order), then the stalest
            due.sort(key=lambda s: (s in self._tried_at, self._tried_at.get(s, 0.0), rank[s]))
            # First fill (after Start or Refresh): go faster, because the scanner's first choices
            # are made blind until this arrives. Steady state stays at per_round.
            never = sum(1 for s in due if s not in self._tried_at)
            todo = due[:self.per_round * (3 if never > self.per_round else 1)]
            for s in todo:
                self._tried_at[s] = now              # also on failure: wait out the TTL, do not hammer
        def fetch(show_id):
            try:
                return show_id, self.client.upcoming_giveaways(show_id)
            except Exception as e:
                log.debug("giveaway list for %s failed: %s", show_id, e)
                return show_id, None

        if self.workers > 1 and len(todo) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(pool.map(fetch, todo))
        else:
            results = [fetch(s) for s in todo]
        done = 0
        for show_id, got in results:
            if got is None:
                continue
            with self._lock:
                if self._listings.get(show_id) != got:
                    self._listings[show_id] = got
                    self.version += 1
            done += 1
        return done

    def coverage(self, show_ids) -> tuple[int, int]:
        """(streams with a list fetched, streams asked about): for the window's readout."""
        with self._lock:
            return sum(1 for s in show_ids if s in self._listings), len(show_ids)

    def summary(self) -> dict[str, Listed]:
        """show_id -> Listed. Streams not looked up yet are ABSENT (the scanner treats that as 'unknown')."""
        with self._lock:
            out = {}
            for s, ls in self._listings.items():
                free = [g for g in ls if not is_buyers_only(g)]
                grade = max((grade_title(g.title) for g in free), default=-1)
                out[s] = Listed(open=len(free), pack=grade == 2, buyers=len(ls) - len(free), grade=grade)
            return out

    def is_buyers_title(self, show_id: str, title: str) -> bool:
        """Is the giveaway with this title (as the card shows it: 'EOS GIVY #3') one of this stream's
        buyers giveaways? False when the stream has not been looked up."""
        want = _norm_title(title)
        with self._lock:
            return bool(want) and any(_norm_title(g.title) == want and is_buyers_only(g)
                                      for g in self._listings.get(show_id, ()))

    def enterable_counts(self) -> dict[str, int]:
        """show_id -> how many listed giveaways you could enter (buyers-only ones do
        not count). Streams not looked up yet are ABSENT, which ranking treats as neutral."""
        with self._lock:
            return {s: sum(1 for g in ls if not is_buyers_only(g)) for s, ls in self._listings.items()}

    def mark_all_stale(self) -> None:
        """Manual refresh: everything is due again. What is already shown stays on
        screen until its replacement arrives."""
        with self._lock:
            self._tried_at.clear()

    def note_seen(self, show_id: str, prize: str) -> None:
        """One of our accounts read this prize title off the screen in that stream."""
        prize = (prize or "").strip()
        if not prize:
            return
        with self._lock:
            if self._seen.get(show_id) != prize:
                self._seen[show_id] = prize
                self.version += 1

    def text(self, show_id: str, full: bool = False) -> str:
        """One line for the list; full=True lists every item (for the selected stream)."""
        with self._lock:
            return describe(self._listings.get(show_id), self._seen.get(show_id, ""), None if full else SHOWN)
