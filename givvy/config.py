"""Configuration: config.toml -> typed dataclasses with defaults."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WhatnotCfg:
    client_version: str = "20260916-0147"
    feeds: list[str] = field(default_factory=lambda: ["pokemon_cards", "riftbound_category", "cyberpunk", "tcg_other"])
    followed_sellers: list[str] = field(default_factory=list)
    home_country: str = "US"          # a card saying '<other country> Only Giveaway' blacklists that seller
    only_home_country: bool = True    # and streams shipping from anywhere else are left out of the list
    discovery_interval_s: int = 60
    watch_slots: int = 40
    feed_page_size: int = 50           # per request; more than 50 returns nothing (measured)
    feed_max_pages: int = 12           # 12 x 50 = up to 600 streams per category


@dataclass
class ScoringCfg:
    min_score: float = 40
    min_score_confirm: float = 60
    min_viewers_to_camp: int = 40   # smaller streams do not run giveaways
    value_w: float = 1.0
    odds_w: float = 1.0
    seller_bonus: float = 30
    favorite_bonus: float = 60      # a streamer you starred: outranks game and odds differences
    listed_bonus: float = 50        # the seller has giveaways listed that you can enter
    # A giveaway is graded by the words in its description (whole words, plural 's' allowed). Surveyed live.
    wanted_words: list[str] = field(default_factory=lambda: [
        "pack", "booster", "box", "etb", "elite trainer", "bundle", "tin", "blister", "sealed", "collection",
        "slab", "psa", "cgc", "bgs", "graded", "display", "upc"])
    unwanted_words: list[str] = field(default_factory=lambda: [
        "card", "sticker", "keychain", "single", "holo", "holofoil", "reverse", "energy", "fan art", "pin", "coin",
        "sleeve", "toploader", "plush", "poster", "promo", "bulk", "playmat", "dice", "magnet", "lanyard",
        "toy", "figure", "squishy"])
    only_wanted_prizes: bool = True        # enter only giveaways that NAME a wanted prize; join only streams listing one
    skip_cards_and_stickers: bool = True   # never join cards/stickers-only streams, never enter such a giveaway
    none_listed_penalty: float = 60 # the seller lists none: usually means none will run
    quiet_penalty: float = 100      # we just sat there and nothing happened (see pacing.quiet_cooldown_s)
    default_prize_value: float = 15
    late_penalty_after_s: int = 45
    late_penalty_per_min: float = 60
    unwitnessed_start_age_s: float = 100
    game_w: dict[str, float] = field(default_factory=lambda: {"riftbound": 100, "pokemon": 80, "cyberpunk": 60, "other": 10})
    prize_keywords: dict[str, float] = field(default_factory=dict)


@dataclass
class PacingCfg:
    max_entries_per_hour: int = 12
    min_idle_between_shows_s: int = 20
    tap_delay_min_s: float = 0.8      # before non-critical taps (Follow, badge)
    tap_delay_max_s: float = 2.0
    enter_tap_delay_min_s: float = 0.15   # before the Enter tap: the card is open and
    enter_tap_delay_max_s: float = 0.4    # lapsing; pacing is enforced by the hourly cap
    open_settle_min_s: float = 2.0
    open_settle_max_s: float = 20.0
    enter_timeout_s: int = 60
    hold_timeout_s: int = 420
    switch_after_quiet_s: int = 240   # scan mode: quiet spell before considering a move
    switch_if_never_active_s: int = 90 # shorter, if it has run nothing since we arrived
    switch_margin: float = 25         # and the new stream must beat this one by this much
    single_quiet_s: int = 900         # 'stay in one stream': leave after this long with no giveaway. Measured on
                                      # 886 giveaways: 90% follow the previous one within 11 min, 95% within 17.
    quiet_cooldown_s: int = 1800      # a stream left for being dead is not picked straight back
    poll_interval_s: float = 1.5      # screen poll while camped (a read costs ~0.45s)
    idle_poll_interval_s: float = 3.0 # after a quiet spell, ease off
    idle_after_s: int = 120           # how long with no card before easing off
    expand_settle_s: float = 0.7      # wait after tapping the badge before the one read
    verify_settle_s: float = 1.0      # wait after tapping Enter before the one read


@dataclass
class DeviceCfg:
    phone_serial: str = ""
    avd_name: str = "givvy"
    android_sdk: str = "C:/Android/sdk"
    emulator_boot_after_phone_missing_s: int = 120
    emulator_stop_after_phone_back_s: int = 120
    whatnot_package: str = "com.whatnot_mobile"
    emulator_mobile_data: bool = False   # off: no Android 'data warning' (the emulator uses its Wi-Fi either way)


@dataclass
class AccountCfg:
    """One device. Devices sharing an `account` label share a Whatnot login and
    are alternatives: only the first enabled, connected one runs."""
    name: str = ""
    kind: str = "phone"          # "phone" | "emulator"
    account: str = ""            # defaults to name
    serial: str = ""             # phone: adb serial
    avd: str = ""                # emulator: AVD name
    port: int = 0                # emulator: console port; serial is emulator-<port>
    enabled: bool = True
    whatnot_username: str = ""   # optional: lets the bot recognise '<you> won the giveaway!'
    cpu_affinity: str = ""       # emulator: pin to logical processors, e.g. "0-3"; blank = not pinned
    priority: str = "normal"     # emulator process priority: normal | below_normal | idle | above_normal


@dataclass
class DiscordCfg:
    bot_token: str = ""
    channel_id: str = ""
    mention: str = ""
    command_poll_s: int = 10


@dataclass
class PathsCfg:
    db: str = "state.db"
    screens_dir: str = "screens"
    log: str = "givvy.log"


@dataclass
class Config:
    whatnot: WhatnotCfg = field(default_factory=WhatnotCfg)
    scoring: ScoringCfg = field(default_factory=ScoringCfg)
    pacing: PacingCfg = field(default_factory=PacingCfg)
    device: DeviceCfg = field(default_factory=DeviceCfg)
    discord: DiscordCfg = field(default_factory=DiscordCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)
    accounts: list[AccountCfg] = field(default_factory=list)
    root: Path = field(default_factory=Path.cwd)


def _fill(cls, data: dict):
    obj = cls()
    for k, v in (data or {}).items():
        if hasattr(obj, k):
            setattr(obj, k, v)
    return obj


def derive_accounts(device: DeviceCfg) -> list[AccountCfg]:
    """The pre-multi-account setup: phone when docked, else the emulator, one login."""
    out = []
    if device.phone_serial:
        out.append(AccountCfg(name="phone", kind="phone", account="main", serial=device.phone_serial))
    if device.avd_name:
        out.append(AccountCfg(name="emulator", kind="emulator", account="main", avd=device.avd_name, port=5554))
    return out


def load(path: Path | str = "config.toml") -> Config:
    path = Path(path)
    with path.open("rb") as f:
        raw = tomllib.load(f)
    cfg = Config(
        whatnot=_fill(WhatnotCfg, raw.get("whatnot")),
        scoring=_fill(ScoringCfg, raw.get("scoring")),
        pacing=_fill(PacingCfg, raw.get("pacing")),
        device=_fill(DeviceCfg, raw.get("device")),
        discord=_fill(DiscordCfg, raw.get("discord")),
        paths=_fill(PathsCfg, raw.get("paths")),
        root=path.resolve().parent,
    )
    cfg.accounts = [_fill(AccountCfg, a) for a in raw.get("accounts", [])] or derive_accounts(cfg.device)
    for a in cfg.accounts:
        a.account = a.account or a.name
    cfg.whatnot.followed_sellers = [s.lower() for s in cfg.whatnot.followed_sellers]
    cfg.scoring.prize_keywords = {k.lower(): float(v) for k, v in cfg.scoring.prize_keywords.items()}
    cfg.scoring.game_w = {k.lower(): float(v) for k, v in cfg.scoring.game_w.items()}
    return cfg
