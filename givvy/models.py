"""Plain data passed between modules."""
from __future__ import annotations

from dataclasses import dataclass, field

GAMES = ("riftbound", "pokemon", "cyberpunk", "other")
# Coarse priority used for ordering shows before any scoring happens (feeds, watch slots).
GAME_ORDER = {"riftbound": 3, "pokemon": 2, "cyberpunk": 1, "other": 0}


@dataclass(frozen=True)
class LiveShow:
    id: str
    seller: str            # username, lowercase
    title: str
    category: str          # e.g. "tag/pokemon_cards" (deeplink of first livestreamCategory)
    game: str               # one of GAMES
    viewers: int
    start_time_ms: int
    source: str             # feed slug or "seller:<username>"
    country: str = ""       # where the seller ships from (Whatnot's shippingSourceCountryCode); "" = not told

    @property
    def url(self) -> str:
        return f"https://www.whatnot.com/live/{self.id}"


@dataclass(frozen=True)
class GiveawayListing:
    live_product_id: str   # salesChannels[].meta.id, type LIVESTREAM_PRODUCT_ID
    listing_id: str
    title: str
    quantity: int
    only_followers: bool
    only_domestic: bool
    buyer_appreciation: bool


@dataclass
class GiveawayEvent:
    show: LiveShow
    live_product_id: str
    status: str                       # "active" | "ended"
    listing: GiveawayListing | None   # None when the prize could not be identified
    started_at: float                 # time.time() when we saw it become active
    viewers: int
    saw_start: bool = True            # False when it was already running as we joined,
                                      # so started_at is a floor, not the real start

    @property
    def prize_text(self) -> str:
        return self.listing.title if self.listing else ""

    @property
    def follower_only(self) -> bool | None:
        return self.listing.only_followers if self.listing else None


@dataclass
class Candidate:
    event: GiveawayEvent
    score: float
    parts: dict[str, float] = field(default_factory=dict)
