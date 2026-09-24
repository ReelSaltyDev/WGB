"""Find live shows worth watching: category feeds + followed sellers."""
from __future__ import annotations

import logging

from .models import GAME_ORDER, LiveShow

log = logging.getLogger(__name__)

CYBER = "cyberpunk"


def classify_game(deeplink: str, title: str, tag_labels: list[str], source: str) -> str:
    dl = (deeplink or "").lower()
    text = (title or "").lower() + " " + " ".join(t.lower() for t in tag_labels)
    if "riftbound" in dl:
        return "riftbound"
    if "pokemon" in dl:
        return "pokemon"
    if source == CYBER or CYBER in text:
        return CYBER
    return "other"


def to_show(obj: dict, source: str) -> LiveShow:
    cats = obj.get("livestreamCategories") or []
    deeplink = cats[0].get("deeplink", "") if cats else ""
    tags = [t.get("label", "") for t in obj.get("tags") or []]
    return LiveShow(
        id=obj["id"], seller=(obj.get("user") or {}).get("username", "").lower(), title=obj.get("title") or "",
        category=deeplink, game=classify_game(deeplink, obj.get("title", ""), tags, source),
        viewers=int(obj.get("activeViewers") or 0), start_time_ms=int(obj.get("startTime") or 0), source=source,
        country=(obj.get("shippingSourceCountryCode") or "").upper())


class Discovery:
    def __init__(self, client, feeds: list[str], followed_sellers: list[str], page_size: int = 50, max_pages: int = 12,
                 home_country: str = ""):
        self.client = client
        # Sellers shipping from another country: tariffs, another currency, often '<country> Only' giveaways.
        # Measured one morning: 171 of 286 Pokemon streams. "" keeps everything.
        self.home_country = (home_country or "").upper()
        self.hidden_abroad = 0
        self.feeds = feeds
        self.followed_sellers = [s.lower() for s in followed_sellers]
        self.page_size = page_size
        self.max_pages = max_pages

    def add_seller(self, username: str) -> None:
        """Also look this seller up directly each poll (a new favorite): the
        category feeds only return the top few dozen streams."""
        u = username.strip().lower()
        if u and u not in self.followed_sellers:
            self.followed_sellers.append(u)

    def poll(self) -> list[LiveShow]:
        found: dict[str, LiveShow] = {}
        for slug in self.feeds:
            try:
                for obj in self.client.live_shows(self.client.feed_id(slug), self.page_size, self.max_pages):
                    show = to_show(obj, slug)
                    if slug == "tcg_other" and show.game != CYBER:
                        continue  # tcg_other is only mined for Cyberpunk shows
                    prev = found.get(show.id)
                    if prev is None or _rank(show) > _rank(prev):
                        found[show.id] = show
            except Exception as e:  # network / schema hiccup: keep going with other feeds
                log.warning("feed %s failed: %s", slug, e)
        for username in list(self.followed_sellers):
            try:
                obj = self.client.seller_live_show(username)
            except Exception as e:
                log.warning("seller %s lookup failed: %s", username, e)
                continue
            if obj:
                show = to_show(obj, f"seller:{username}")
                if show.id not in found:
                    found[show.id] = show
        if self.home_country:
            home = [s for s in found.values() if not s.country or s.country == self.home_country]
            self.hidden_abroad = len(found) - len(home)
            return sorted(home, key=_rank, reverse=True)
        self.hidden_abroad = 0
        return sorted(found.values(), key=_rank, reverse=True)


def _rank(show: LiveShow) -> tuple:
    return (GAME_ORDER.get(show.game, 0), show.viewers)
