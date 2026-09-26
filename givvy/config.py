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
    feeds_first: bool = True          # on a start (no stream list yet), hand the accounts the category feeds before the
                                      # ~250 followed-seller lookups, one at a time, that held the first pick 79-89 s
                                      # (9/23-24); false = one list at the end, as in 1.1.x
    watch_slots: int = 40
    feed_page_size: int = 50           # per request; more than 50 returns nothing (measured)
    feed_max_pages: int = 12           # 12 x 50 = up to 600 streams per category


@dataclass
class ScoringCfg:
    min_score: float = 40
    min_score_confirm: float = 60
    min_viewers_to_camp: int = 40   # smaller streams do not run giveaways
    quiet_max_per_h: float = 0.5    # most giveaways/h a stream scores after 10+ min of listened silence: under the
                                    # 2.0/h guess for one never watched (3 or more = the 1.1.x formula)
    quiet_memory_s: int = 2700      # keep 'quiet while we listened' this long after a stream leaves the 40 listener
                                    # slots; time away is not quiet (0 = forget at once, as in 1.1.x)
    crowd_from_seen: bool = True    # counts read on cards refine the expected crowd at the draw (false = viewers only)
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
    one_row_per_giveaway: bool = True # a card back in our stream soon after our last row there, count not collapsed, may
                                      # be that giveaway: read it before any tap, no second row, no second start for
                                      # the ActivityBook (false = 1.1.x: fast-tap it and record it again)
    same_giveaway_s: int = 600        # 'soon' above. Repeats came 11-252 s after the first row (9/23-24). Keep it
                                      # above hold_timeout_s: that re-look must not fast-tap a giveaway we are in
    skip_buyers_without_button: bool = True  # an open buyers card with no Enter button: 'skipped' once, and hold
                                             # (false = 1.1.x: four reads, then a 'missed' row)
    switch_after_quiet_s: int = 240   # scan mode: quiet spell before considering a move
    switch_if_never_active_s: int = 90 # shorter, if it has run nothing since we arrived
    switch_margin: float = 25         # read nowhere; kept so older config.toml files mean the same
                                      # (see pack_upgrade_ratio)
    pack_upgrade_ratio: float = 1.5   # leave a mixed/other stream for a pack-only one only when it promises this many
                                      # times the pack wins/h, same crowd model both sides; 0 = any free pack-only
                                      # stream, as in 1.1.x
    upgrade_after_draw: bool = True   # ...and never while our entry there may be open: after its draw (the socket's, or
                                      # a new giveaway on screen), at most hold_timeout_s; with [hop] live it is decided
                                      # at that draw and goes after the linger, like a hop. false = 1.1.x: at the first
                                      # read with no card (14 of 27 upgrades 9/20-9/24 came within 240 s of an entry)
    resume_after_restart: bool = True # save each account's stream in state.db and go straight back to it after an app
                                      # restart, firing its stream link again (9/23-24: 10 of 16 account-restarts landed
                                      # on another seller); false = only after an emulator restart, and only once the
                                      # stream list has it, as in 1.1.x
    resume_within_s: int = 900        # go back only if it was in that stream this recently (app or emulator restart)
    check_live_before_leaving: bool = True  # a stream missing from one full stream list is left only when Whatnot says
                                            # it is not live (false = 1.1.x: leave on the first miss)
    single_quiet_s: int = 600         # 'stay in one stream': after this long with no giveaway the stay is over and the
                                      # account goes back to Scan (John, 2026-09-25: "10 min of no givvies"). Measured
                                      # on 886 giveaways: 90% follow the previous one within 11 min, 95% within 17.
    quiet_cooldown_s: int = 1800      # a stream left for being dead is not picked straight back
    poll_interval_s: float = 1.5      # screen poll while camped (a read costs ~0.45s)
    idle_poll_interval_s: float = 3.0 # after a quiet spell, ease off
    idle_after_s: int = 120           # how long with no card before easing off
    expand_settle_s: float = 0.7      # wait after tapping the badge before the one read
    verify_settle_s: float = 1.0      # wait after tapping Enter before the one read


@dataclass
class HopCfg:
    """The crowd hop (1.2.0 item E, givvy/hop.py): leave a crowded stream right after its draw when a clearly less
    crowded one is free. 9/19-9/24: stays whose first read was over 30 entrants took 26-28% of camped time at about 0.11
    pack wins/h, first reads of 10 or fewer about 0.63/h. It raises joins from about 2 to at most about 3 per
    account-hour, hence the cap and the one switch. pacing.switch_margin is not read here."""
    mode: str = "live"          # live | log | off (anything else = off). THE switch for the crowd hop. log: judge every
                                # draw and write the decision (hop_decisions), never move; off: as 1.1.x, nothing
    margin: float = 1.5         # leave only for a stream promising this many times the pack wins/h of this one, same
                                # crowd model on both sides (the proposal's range was 1.5-2x)
    min_crowd: float = 30.0     # never hop out of a stream whose expected final crowd is under this: about a first
                                # read of 13-15 at 25-30 viewers, or 35 viewers unread (first reads of 10 or fewer won
                                # 0.63/h, 11-30 0.27/h). Keeps good small streams safe from noisy reads
    linger_s: int = 25          # stay this long after the draw (the socket's 'ended'): the win dialog is seen and the
                                # move is not instant. Nothing new is entered meanwhile (John: 20-30 s)
    fresh_s: int = 120          # the stream moved to must have a giveaway the socket SAW start at most this long before
                                # the move (they run a median 204-251 s; a join takes a median 5.9 s)
    max_per_hour: int = 3       # voluntary moves (crowd hops and pack-only upgrades) per account in any 3600 s, counted
                                # from hop_decisions so a restart does not reset it. 0 = never hop


@dataclass
class DeviceCfg:
    phone_serial: str = ""
    avd_name: str = "givvy"
    android_sdk: str = "C:/Android/sdk"
    emulator_boot_after_phone_missing_s: int = 120
    emulator_stop_after_phone_back_s: int = 120
    whatnot_package: str = "com.whatnot_mobile"
    record: bool = False                 # record every screen read and tap to recordings/ for replay tests
    fast_reader: bool = True             # the persistent on-device screen reader (device/reader.py); false = old way
    emulator_mobile_data: bool = False   # off: no Android 'data warning' (the emulator uses its Wi-Fi either way)
    emulator_retry_s: int = 60           # wait after a failed boot, counted from when it FAILED (9/22 and 9/24 the retry
                                         # came 60 s after a 240 s timeout); doubles with each failure in a row
    emulator_retry_max_s: int = 1800     # the longest wait: the bot never gives up (the 9/22 outage ended by itself)
    emulator_alert_after: int = 3        # failed boots in a row before one alert; the next good boot resolves it
    emulator_kill_leftover: bool = True  # 'already running' and adb does not list it, or Power off / Restart on one adb
                                         # does not list: end the leftover process for this AVD and cold-boot (9/24:
                                         # two leftovers held both AVDs 04:30-06:10 and every retry failed)


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
    alerts_channel_id: str = ""     # a channel for alerts only, with the mention: routine posts run about 700 a day and
                                    # would bury them. Blank = channel_id when that is set, else no Discord


@dataclass
class PathsCfg:
    db: str = "state.db"
    screens_dir: str = "screens"
    screens_keep_days: float = 7     # the bot's screenshots are deleted after this many days (0 = keep them)
    screens_max_mb: float = 300      # ...and the oldest go first when the folder is bigger than this (0 = no limit)
    log: str = "givvy.log"
    # Optional: the Givvy Wins tracker's database (Whatnot's own order history). When set, Win Charts takes its wins
    # from there every 5 minutes; the bot only sees the wins that happen on screen while it watches.
    givvy_wins_db: str = ""
    alerts_log: str = "alerts.log"   # one ALERT or RESOLVED line per event, beside givvy.log (works without Discord)


@dataclass
class TrialCfg:
    """The 1.2.0 trial against 1.1.x: what is recorded so the two can be compared, and the two windows the trial report
    (givvy/trial.py) compares. The times are read by the report, never here: a typo in one cannot stop the app starting,
    it is ignored and named in the report."""
    record_visits: bool = True       # one visits row per stay and per failed stream open, for the trial report
                                     # (false = those numbers stay blank)
    auto_mark: bool = True           # the first Start on this version, with no trial start stored, stores one (kv
                                     # trial_start, with the version and a snapshot of the settings)
    start: str = ""                  # trial start, local 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD': overrides the stored one
                                     # (skip a warm-up hour, or restart the window after a change such as E switched off;
                                     # the automatic baseline stays before this version's first start)
    end: str = ""                    # end of the trial window (e.g. the moment of a revert); blank = now
    baseline_start: str = ""         # start of the baseline window; blank = automatic
    baseline_days: float = 7.0       # the automatic baseline: this many days before this version first started (kv
                                     # trial_first_start, never moved), never before 2026-09-20 00:00, when the pack-only
                                     # rules went live


@dataclass
class AlertsCfg:
    """What reaches you when something needs you. 2026-09-24 04:30-06:10 both emulators were down unheard: Discord was
    blank, so every alert was only a line in givvy.log."""
    windows_notify: bool = True      # a Windows notification from a red tray icon for each alert and resolution
    out_of_stream_min: int = 15      # an enabled account you did not pause out of a stream (or its loop not ticking)
                                     # this long: one alert (0 = off)


@dataclass
class SafetyCfg:
    """The account circuit breaker (givvy/camper.py Breaker): stream links failing on several sellers point at the
    account, not the streams. The 9/22 jswike suspension made 26 failed opens on 9 sellers in 20 minutes, unnoticed.
    And a stream link that lands on Whatnot's sign-in screen, which is looked for first (Camper._sign_in_on_open)."""
    breaker: bool = True             # pause the account and alert; it never switches account, device or connection
    breaker_failures: int = 5        # failed opens within breaker_window_s that trip it (9/22: the 5th at 12:06:32)
    breaker_sellers: int = 4         # ...on at least this many sellers (a 3-in-2 rule tripped falsely 9/19, on 3)
    breaker_window_s: int = 600      # worst 10 minutes outside the suspension, 9/16-9/24 am: 4 failures on 3 sellers
    breaker_recheck_s: int = 1200    # after a trip one stream is tried after this long; each failed try doubles it
    breaker_recheck_max_s: int = 7200  # ...up to 2 h: a suspended account costs one stream link per try, not 26
    check_sign_in_on_open: bool = True  # a stream link that lands on Whatnot's sign-in screen pauses the account for a
                                        # sign-in, as that screen in a stream does, and it resumes by itself once signed
                                        # in (9/24 12:29-12:40, signed out, 15 stream links failed instead); false = try
                                        # the next stream, as before


@dataclass
class Config:
    whatnot: WhatnotCfg = field(default_factory=WhatnotCfg)
    scoring: ScoringCfg = field(default_factory=ScoringCfg)
    pacing: PacingCfg = field(default_factory=PacingCfg)
    hop: HopCfg = field(default_factory=HopCfg)
    device: DeviceCfg = field(default_factory=DeviceCfg)
    discord: DiscordCfg = field(default_factory=DiscordCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)
    trial: TrialCfg = field(default_factory=TrialCfg)
    alerts: AlertsCfg = field(default_factory=AlertsCfg)
    safety: SafetyCfg = field(default_factory=SafetyCfg)
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
        hop=_fill(HopCfg, raw.get("hop")),
        device=_fill(DeviceCfg, raw.get("device")),
        discord=_fill(DiscordCfg, raw.get("discord")),
        paths=_fill(PathsCfg, raw.get("paths")),
        trial=_fill(TrialCfg, raw.get("trial")),
        alerts=_fill(AlertsCfg, raw.get("alerts")),
        safety=_fill(SafetyCfg, raw.get("safety")),
        root=path.resolve().parent,
    )
    cfg.accounts = [_fill(AccountCfg, a) for a in raw.get("accounts", [])] or derive_accounts(cfg.device)
    for a in cfg.accounts:
        a.account = a.account or a.name
    cfg.whatnot.followed_sellers = [s.lower() for s in cfg.whatnot.followed_sellers]
    cfg.scoring.prize_keywords = {k.lower(): float(v) for k, v in cfg.scoring.prize_keywords.items()}
    cfg.scoring.game_w = {k.lower(): float(v) for k, v in cfg.scoring.game_w.items()}
    return cfg
