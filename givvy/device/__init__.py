"""Device protocol: what the orchestrator needs from a phone-like thing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class GiveawayScreen:
    card_visible: bool            # a giveaway card/badge is on screen
    entered: bool                 # the card says we're entered
    entries: int | None           # "N entries" if shown
    follow_required: bool         # card says follow to enter / Follow button present with a follower-only giveaway
    prize: str                    # card title, readable only once the card is expanded; "" otherwise
    won: bool                     # "You won" / winner dialog visible
    login_needed: bool            # login/sign-up screen visible
    follow_on_enter: bool = False # the button reads 'Follow Host & Enter Giveaway',
                                  # i.e. one tap both follows and enters
    overlay: bool = False         # a sheet/modal is covering the stream; dismiss it first
    tariff_notice: bool = False   # 'additional tariffs will apply': seller ships from abroad
    region_only: str = ""         # 'AU Only Giveaway' on the card -> "AU"; "" when there is no such pill


class Device(Protocol):
    name: str

    def is_available(self) -> bool: ...
    def is_user_busy(self) -> bool: ...
    def open_show(self, url: str) -> str | None: ...   # 'ok' | 'tariff' | 'missing'
    def join_past_notice(self) -> bool: ...
    def current_show(self) -> str | None: ...
    def giveaway_state(self) -> GiveawayScreen: ...
    def expand_card(self) -> bool: ...
    def snapshot(self) -> tuple[list[dict], str]: ...   # (ui nodes, foreground package)
    def dismiss_overlay(self) -> bool: ...
    def follow_seller(self) -> bool: ...
    def enter_giveaway(self) -> bool: ...
    def screenshot(self, path: str) -> str: ...
    def go_home(self) -> None: ...
