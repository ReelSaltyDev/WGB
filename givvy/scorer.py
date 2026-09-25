"""Weighted scoring of active giveaways. Pure functions."""
from __future__ import annotations

import re
import time

from .config import ScoringCfg
from .models import Candidate, GiveawayEvent

_DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def prize_value(text: str, cfg: ScoringCfg) -> float:
    t = (text or "").lower()
    m = _DOLLARS.search(t)
    if m:
        return float(m.group(1).replace(",", ""))
    hits = [v for k, v in cfg.prize_keywords.items() if k in t]
    return max(hits) if hits else float(cfg.default_prize_value)


def odds(entries: int | None, viewers: int) -> float:
    n = entries if entries is not None else viewers
    return min(100.0, 1000.0 / (max(0, n) + 1))


def score_confirmed(event: GiveawayEvent, prize_text: str, entries: int | None, cfg: ScoringCfg,
                    followed: set[str], now: float | None = None) -> tuple[float, dict[str, float]]:
    """Score a giveaway from an explicit prize title and entry count.

    The web API exposes neither, so at pre-selection both are absent and this is
    called via score() with an empty title and None entries. The orchestrator
    calls it again once the device is in the show and has read the real values.
    """
    now = time.time() if now is None else now
    parts = {
        "game": float(cfg.game_w.get(event.show.game, cfg.game_w.get("other", 0))),
        "value": cfg.value_w * prize_value(prize_text, cfg),
        "odds": cfg.odds_w * odds(entries, event.viewers),
        "seller": float(cfg.seller_bonus) if event.show.seller in followed else 0.0,
    }
    # A giveaway that was already running when we joined has an unknown real start,
    # so assume it is half-way through a typical run (measured median 204s). Without
    # this, everything looks brand new at startup and the device chases giveaways
    # that end before it arrives.
    elapsed = now - event.started_at + (0.0 if event.saw_start else cfg.unwitnessed_start_age_s)
    late_s = max(0.0, elapsed - cfg.late_penalty_after_s)
    parts["late"] = -cfg.late_penalty_per_min * (late_s / 60.0)
    parts = {k: round(v, 3) for k, v in parts.items()}   # parts are logged and reported; keep them tidy
    return round(sum(parts.values()), 3), parts


def score(event: GiveawayEvent, entries: int | None, cfg: ScoringCfg, followed: set[str],
          now: float | None = None) -> tuple[float, dict[str, float]]:
    return score_confirmed(event, event.prize_text, entries, cfg, followed, now)


def rank(events: list[GiveawayEvent], entries_by_id: dict[str, int], cfg: ScoringCfg,
         followed: set[str]) -> list[Candidate]:
    out = []
    for e in events:
        s, parts = score(e, entries_by_id.get(e.live_product_id), cfg, followed)
        if s >= cfg.min_score:
            out.append(Candidate(e, s, parts))
    out.sort(key=lambda c: c.score, reverse=True)
    return out
