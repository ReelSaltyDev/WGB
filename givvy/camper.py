"""Camp in one stream and enter its giveaways.

Why camping rather than chasing: a giveaway is visibly open for a median of 204s,
but detecting it over the socket costs up to ~30s and navigating the phone costs
another ~20s, and the card must then be expanded before the button exists. Live
runs that hopped between streams went 0 for 24 -- every single one arrived after
the draw. Parked in a stream, the phone sees the card the moment it appears and
the whole entry is a couple of taps.

Two modes:
  single  park in one chosen stream and never leave
  scan    park in the best stream, and only when nothing is running there,
          consider moving to a better one
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .models import GAME_ORDER, LiveShow
from .notify import format_entry
from .scorer import odds, prize_value, score_confirmed
from .store import Store

log = logging.getLogger(__name__)

MAX_EXPANDS = 4            # the expanded card re-collapses on its own


@dataclass
class StreamPick:
    show: LiveShow
    score: float
    reason: str
    tier: tuple = ()               # strict priorities, compared before the score (see rank_streams)
    excluded: str = ""             # "buyers only": never chosen; listed in the window only
    pack: bool = False             # an open giveaway here has 'pack' in its description
    buyers: int = 0                # buyers giveaways listed here (the bot skips those cards)
    kind: str = ""                 # givvy.chooser kind: pack_only / mixed / other / junk / ...


@dataclass
class CamperState:
    """Everything the UI needs to render, in one place."""
    mode: str = "scan"                     # "scan" | "single"
    camped: LiveShow | None = None
    camped_since: float = 0.0
    last_action: str = "idle"
    entered_here: int = 0
    giveaways_seen_here: int = 0
    paused: bool = False
    paused_reason: str = ""            # "login": the bot paused itself and will look again; "": you did
    device_name: str | None = None
    pinned_url: str | None = None          # single mode: the stream the user chose
    recent: list[str] = field(default_factory=list)
    tag: str = ""                          # account label, shown when several accounts run
    sink: object = None                    # callable(line): the window's merged activity log

    def note(self, msg: str):
        self.last_action = msg
        line = f"{time.strftime('%H:%M:%S')}  {'[' + self.tag + '] ' if self.tag else ''}{msg}"
        self.recent.append(line)
        del self.recent[:-40]
        if self.sink is not None:
            try:
                self.sink(line)
            except Exception:
                pass
        log.info("[%s] %s", self.tag, msg) if self.tag else log.info(msg)


def rank_streams(shows: list[LiveShow], giveaway_counts: dict[str, int], cfg: Config,
                 followed: set[str], blocked: set[str] | frozenset = frozenset(),
                 skip_ids: set[str] | frozenset = frozenset(),
                 favorites: set[str] | frozenset = frozenset(),
                 listed: dict | None = None,
                 cooling: set[str] | frozenset = frozenset(), keep_excluded: bool = False,
                 activity=None, now: float | None = None) -> list[StreamPick]:
    """Streams worth sitting in, best first. The rules are in givvy/chooser.py:
    not just left for being dead > kind (pack_only, mixed, other) > expected wins per hour > fewer viewers.
    Junk-only, buyers-only, nothing-listed and not-yet-read streams are never joined (`keep_excluded` lists
    them last, with the reason, for the window). `favorites` and `followed` no longer move the scanner:
    favorites are a watchlist. With no giveaway lists at all (none wired, or none arriving) every stream
    counts as 'legacy' so the scanner still works."""
    from . import chooser
    now = time.time() if now is None else now
    legacy = not listed
    out: list[StreamPick] = []
    for s in shows:
        if s.id in skip_ids or s.seller in blocked:     # another of our accounts is in it / blacklisted
            continue
        if s.viewers < cfg.scoring.min_viewers_to_camp:
            continue
        info = None if legacy else listed.get(s.id)
        if isinstance(info, int):                       # a plain count (older callers): something / nothing
            kind = chooser.OTHER if info > 0 else chooser.NOTHING
        else:
            kind = chooser.LEGACY if legacy else (info.kind or chooser.UNKNOWN if info is not None else chooser.UNKNOWN)
        why_not = chooser.WHY_NOT.get(kind, "")
        if why_not and not keep_excluded:
            continue
        val = chooser.value_of(s, activity, now, giveaway_counts.get(s.id, 0))
        tier = (0 if why_not else 1, 0 if s.id in cooling else 1, chooser.KIND_RANK.get(kind, 0),
                round(val.wins_per_hour, 4), -s.viewers)
        out.append(StreamPick(s, round(val.wins_per_hour, 3),
                              f"{chooser.LABEL.get(kind, kind) or 'stream'}: {val.text()}, {s.viewers} viewers"
                              + (", just left for being quiet" if s.id in cooling else ""),
                              tier=tier, excluded=why_not, pack=kind == chooser.PACK_ONLY,
                              buyers=0 if info is None or isinstance(info, int) else info.buyers, kind=kind))
    out.sort(key=lambda p: p.tier, reverse=True)
    return out


class Camper:
    def __init__(self, cfg: Config, store: Store, notifier, device_getter, watcher=None,
                 followed: set[str] | None = None, account: str = "main", claims=None):
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.device_getter = device_getter
        self.watcher = watcher
        self.account = account
        self.claims = claims           # shared registry: one of our accounts per stream
        self.followed = set(followed or ()) | set(store.followed_sellers(account))
        self.state = CamperState()
        self.shows: list[LiveShow] = []
        self.giveaway_counts: dict[str, int] = {}
        self._idle_since = 0.0
        self._handled = False          # have we already acted on the card on screen? (see _card_handled)
        self._handled_at = 0.0
        self._handled_max: int | None = None   # highest entry count seen while 'handled'
        self._lost_reads = 0           # consecutive reads that were not a stream at all
        self._no_card_reads = 0        # consecutive reads with no card (debounce)
        # 'Is this seller still live?' (seller, show_id) -> True / False / None = could not tell.
        # The category list is only re-read every few minutes; this is what gets us out of an
        # ended stream quickly. Wired by the engine; None in most tests.
        self.live_check = None
        self.activity = None                # givvy.chooser.ActivityBook, shared by all accounts (wired by the engine)
        self._upgrade_checked = 0.0
        # (show_id, title on the card) -> True when Whatnot flags that listing as a buyers giveaway.
        # Some carry no such word ('EOS GIVY'), and the card on screen does not show the flag.
        self.buyers_title_lookup = None
        self.prize_grade_lookup = None      # (show_id, title on the card) -> 2 wanted / 1 unknown / 0 unwanted
        self.prize_desc_lookup = None       # (show_id, title on the card) -> the listing's description, if it adds anything
        self._live_checked = 0.0
        self._ended: dict[str, float] = {}     # show id -> forget after; the list may still carry it
        self._win_announced = False    # one alert per win, not one per read
        self.on_prize_seen = None      # callable(show_id, prize): the window shows it in the stream list
        self.listed_lookup = None      # callable() -> {show_id: enterable giveaways listed}
        self.cooldowns: dict[str, float] = {}   # show_id -> until; shared between accounts by the engine
        self._not_won_reads = 0
        Path(cfg.paths.screens_dir).mkdir(parents=True, exist_ok=True)

    # ---- inputs ----
    def set_shows(self, shows: list[LiveShow]):
        self.shows = shows

    def wanted_poll_interval(self) -> float:
        """Poll fast while something could be happening; ease off after a long
        quiet spell. Every poll is a screen read on the phone."""
        p = self.cfg.pacing
        if self.state.camped is None:
            return p.poll_interval_s
        if self._card_handled:
            # Holding: we have entered (or decided about) the card on screen and
            # nothing changes until it goes away. Each read costs ~2.6s on a live
            # stream, so do not spend one every couple of seconds for nothing.
            return p.idle_poll_interval_s
        if self._idle_since and time.time() - self._idle_since > p.idle_after_s:
            return p.idle_poll_interval_s
        return p.poll_interval_s

    def note_giveaway(self, show_id: str):
        """Called by the watcher when any stream starts a giveaway: pure statistics
        used to decide where it is worth sitting."""
        self.giveaway_counts[show_id] = self.giveaway_counts.get(show_id, 0) + 1

    # ---- controls the UI drives ----
    def set_mode(self, mode: str):
        self.state.mode = mode
        self.state.note(f"mode set to {mode}")
        if mode == "scan":
            # Seen live: after switching to Scan, the old pin pulled one account back into the
            # same stream on every move.
            self.state.pinned_url = None

    def blacklist_current(self, reason: str = "by hand") -> str | None:
        """The window's 'Blacklist stream' button: the seller we are sitting in, gone from the scanner for
        good, and move on now. Returns the seller, or None when we are not in a stream."""
        show = self.state.camped
        if show is None:
            return None
        self.store.blacklist_add(show.seller, reason)
        self.state.note(f"BLACKLISTED {show.seller}: {reason}")
        self._unpin(show)                   # single mode would walk straight back into a pinned one
        self.state.camped = None            # step() picks the next stream on the next tick
        return show.seller

    def _unpin(self, show) -> None:
        """A pin on a stream that ended or died would only drag us back to it."""
        if self.state.pinned_url and show.id in self.state.pinned_url:
            self.state.pinned_url = None

    def pin(self, url: str | None):
        self.state.pinned_url = url
        if url:
            self.state.note(f"pinned to {url}")
            self.state.camped = None        # force a move to the pinned stream

    LOGIN_RECHECK_S = 20
    LIVE_CHECK_S = 20      # while idle in a stream: how often to ask whether it is still live
    BLIND_WAIT_S = 30      # how long the scanner waits for giveaway lists before choosing without them

    def pause(self, yes: bool):
        self.state.paused = yes
        self.state.paused_reason = ""               # pressed by you: stays until you press Resume
        if yes:
            self._release()
        self.state.note("paused" if yes else "resumed")

    def _recheck_login(self, device, now: float):
        """The bot paused itself at Whatnot's sign-in screen. Nothing used to look
        again, so after you signed in it sat paused forever. Look every 20s."""
        st = self.state
        if now - getattr(self, "_login_checked", 0.0) < self.LOGIN_RECHECK_S:
            return
        self._login_checked = now
        try:
            if not device.is_available():
                return
            ns, _pkg = device.snapshot()
            s, _xy, _ = device._screen_from(ns)
        except Exception as e:
            log.warning("sign-in recheck failed: %s", e)
            return
        if s.login_needed:
            st.last_action = f"waiting for you to sign in to Whatnot on {device.name}"
            return
        st.paused, st.paused_reason = False, ""
        st.camped = None                             # start clean: pick a stream and open it
        st.note("signed in; resuming")
        self.notifier.plain(f"Signed in on {device.name}; resuming")

    def _rank_extras(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        for sid in [s for s, until in list(self.cooldowns.items()) if until <= now]:
            self.cooldowns.pop(sid, None)
        listed = None
        if self.listed_lookup is not None:
            try:
                listed = self.listed_lookup()
            except Exception:
                listed = None
        return {"listed": listed, "cooling": set(self.cooldowns), "activity": self.activity, "now": now}

    def _release(self):
        if self.claims is not None:
            self.claims.release(self.account)

    @property
    def _card_handled(self) -> bool:
        return self._handled

    @_card_handled.setter
    def _card_handled(self, value: bool) -> None:
        if value and not self._handled:
            self._handled_at = getattr(self, "_now", None) or time.time()
            self._handled_max = None
        if not value:
            self._handled_why = ""
        self._handled = bool(value)

    def _new_giveaway_behind_the_same_badge(self, s, now: float) -> bool:
        """Seen live (skytreerips, giveaways back to back every five minutes): the badge never leaves the
        screen, so 'the card went away' never happens and #30 to #35 were taken for #29. Within one
        giveaway the entry count climbs (bar small dips); when it COLLAPSES, a new one has started. Nothing stays
        'handled' beyond hold_timeout_s: look again (an entry we already have just reads 'already entered')."""
        if s.entries is not None:
            # Measured live the day this shipped: the count also DIPS by one or two inside a giveaway
            # (26 -> 25, 23 -> 21; eleven false alarms in ten minutes). A new giveaway starts again near
            # zero, so it must at least halve and fall by three.
            top = self._handled_max
            if top is not None and top - s.entries >= 3 and s.entries * 2 <= top:
                log.info("[%s] entry count fell %s -> %s: a new giveaway", self.account, self._handled_max, s.entries)
                return True
            self._handled_max = s.entries if self._handled_max is None else max(self._handled_max, s.entries)
        return now - self._handled_at > self.cfg.pacing.hold_timeout_s

    UPGRADE_CHECK_S = 30       # in a fallback stream: how often to look for a pack-only one
    RESUME_WITHIN_S = 900      # an emulator back within this long goes back to the stream it was in

    def _remember_for_resume(self, now: float) -> None:
        """The emulator has gone away (Restart pressed because the stream got laggy, a crash, a reboot).
        Remember where we were: otherwise the account picks again, and not always the same stream."""
        if self.state.camped is not None:
            self._resume = (self.state.camped.id, now)

    def _kind(self, show) -> str:
        """givvy.chooser kind of a stream, from its giveaway list as known right now."""
        from . import chooser
        if self.listed_lookup is None:
            return chooser.LEGACY
        try:
            listed = self.listed_lookup()
        except Exception:
            return chooser.LEGACY
        if not listed:
            return chooser.LEGACY                      # no lists at all: the scanner carries on without them
        info = listed.get(show.id)
        return (getattr(info, "kind", "") or chooser.UNKNOWN) if info is not None else chooser.UNKNOWN

    def _judge(self, show, prize: str) -> str:
        """enter / buyers / skip / leave, for the giveaway whose card is open. See givvy/chooser.py."""
        from . import chooser
        from .giveaway_info import grade_title, says_buyers
        flagged = False
        if self.buyers_title_lookup is not None and prize:
            try:
                flagged = bool(self.buyers_title_lookup(show.id, prize))
            except Exception:
                flagged = False
        if flagged or says_buyers(prize):
            return "buyers"
        grade = grade_title(prize)
        if self.prize_grade_lookup is not None and prize:
            try:
                grade = self.prize_grade_lookup(show.id, prize)     # the listing's description counts too
            except Exception:
                pass
        if grade == 2:
            return "enter"
        kind = self._kind(show)
        if kind == chooser.PACK_ONLY:
            return "leave"                             # it was listed as packs only, and this is not one
        if kind == chooser.LEGACY:
            return "skip"                              # no lists to go by: only a card that NAMES a pack or better
        # A fallback stream, used because no pack-only stream was free: 'non-pack items are better than
        # nothing', but never junk.
        return "enter" if grade == 1 else "skip"

    def _live_shows(self) -> list[LiveShow]:
        """The discovered list minus streams we have ourselves seen end."""
        if not self._ended:
            return self.shows
        now = getattr(self, "_now", None) or time.time()
        self._ended = {i: t for i, t in self._ended.items() if t > now}
        return [s for s in self.shows if s.id not in self._ended]

    def _still_live(self, show, now: float) -> bool:
        """Asked at most every LIVE_CHECK_S, and only while nothing is happening on screen."""
        if self.live_check is None or now - self._live_checked < self.LIVE_CHECK_S:
            return True
        self._live_checked = now
        try:
            answer = self.live_check(show.seller, show.id)
        except Exception as e:                      # no answer is not 'ended'
            log.warning("live check for %s failed: %s", show.seller, e)
            return True
        return answer is not False

    def _others(self) -> set[str]:
        return self.claims.held_by_others(self.account) if self.claims is not None else set()

    # ---- the loop ----
    def step(self, now: float | None = None):
        now = time.time() if now is None else now
        self._now = now
        st = self.state
        device = self.device_getter()
        st.device_name = device.name if device else None
        if st.paused and st.paused_reason == "login" and device is not None:
            self._recheck_login(device, now)
            return
        if st.paused or device is None:
            if device is None:
                self._remember_for_resume(now)
                st.camped = None
                st.last_action = "no device connected for this account"
            self._release()
            return
        if not device.is_available():
            # Undocking the phone used to leave the camper hammering a device that
            # was not there: every tick tried to navigate and logged a failure.
            st.last_action = f"{device.name} not connected - waiting"
            self._remember_for_resume(now)
            st.camped = None            # whatever we were in, we are not in it now
            self._release()
            return
        if hasattr(device, "app_installed") and not device.app_installed():
            st.last_action = f"{device.name}: install Whatnot and sign in on it (or it is still booting)"
            st.camped = None
            self._release()
            return
        if st.camped is None and device.is_user_busy():
            # Only pay for the two dumpsys calls when we are about to navigate;
            # while camped, the screen read below answers this for free.
            st.last_action = "waiting: you are using the phone"
            return

        # Stay put. A move only happens when camped is None, which is set on
        # startup, when the stream we are in ends, when the user pins a different
        # one, or when scan mode decides a quiet stream is worth leaving.
        camped = st.camped                       # snapshot: the UI thread can clear this
        if camped is not None and self.shows and all(x.id != camped.id for x in self.shows):
            st.note(f"{camped.seller} is no longer live")
            self._unpin(camped)
            st.camped = camped = None

        if camped is not None and not self._claim(camped):
            # a pause released our claim and another account moved in meanwhile
            st.note(f"{camped.seller} is now held by {self.claims.holder(camped.id)}; leaving it")
            st.camped = camped = None

        if camped is None:
            self._release()                      # not in a stream: hold nothing
            target = self._pick_stream()
            if target is None:
                if not st.last_action.startswith(("pinned stream is held", "checking which streams")):
                    st.last_action = "no suitable stream live"
                return
            self._move_to(device, target, now)
            return

        self._watch_here(device, now)

    def _pick_stream(self) -> LiveShow | None:
        """Only called when we are not camped anywhere."""
        st = self.state
        blocked = self.store.blacklisted_sellers()
        shows = self._live_shows()
        resume = getattr(self, "_resume", None)
        if resume is not None:
            self._resume = None
            now_ = getattr(self, "_now", None) or time.time()
            back = next((s for s in shows if s.id == resume[0]), None)
            if (back is not None and now_ - resume[1] <= self.RESUME_WITHIN_S and back.seller not in blocked
                    and self._claim(back)):
                st.note(f"back after a restart: rejoining {back.seller}")
                return back
        if st.pinned_url:
            for s in shows:
                # a pin left over from single mode must not drag the scanner back
                # into a blacklisted stream, which would loop: open, leave, open...
                if s.id in st.pinned_url and (st.mode == "single" or s.seller not in blocked):
                    if self._claim(s):
                        return s
                    # One entry per person per giveaway: two of our accounts are
                    # never in the same stream, even when asked.
                    st.last_action = f"pinned stream is held by {self.claims.holder(s.id)}; not joining"
                    if st.mode == "single":
                        return None
        now = getattr(self, "_now", None) or time.time()
        extras = self._rank_extras(now)
        if self.listed_lookup is not None and self.shows:
            # Right after a start no giveaway list has arrived yet, and choosing blind
            # sent the scanner into a dead stream on every launch. They take seconds.
            favs = set(self.store.favorites())
            cands = [s for s in shows if s.seller not in blocked and
                     (s.viewers >= self.cfg.scoring.min_viewers_to_camp or s.seller.lower() in favs)][:40]
            known = sum(1 for s in cands if s.id in (extras["listed"] or {}))
            if cands and known < 0.6 * len(cands):
                self._blind_since = getattr(self, "_blind_since", None) or now
                if now - self._blind_since < self.BLIND_WAIT_S:
                    st.last_action = "checking which streams have giveaways listed..."
                    return None
            else:
                self._blind_since = None
        ranked = rank_streams(shows, self.giveaway_counts, self.cfg, self.followed, blocked,
                              self._others(), set(self.store.favorites()), **extras)
        for pick in ranked:
            if self._claim(pick.show):           # can lose a race to another account: try the next
                return pick.show
        return None

    def _claim(self, show: LiveShow) -> bool:
        return self.claims is None or self.claims.claim(show.id, self.account)

    def _move_to(self, device, show: LiveShow, now: float):
        st = self.state
        st.note(f"moving to {show.seller} ({show.game}, {show.viewers} viewers)")
        try:
            arrived = device.open_show(show.url)
        except Exception as e:
            self._error(device, "open_show", e)
            self._release()
            return
        if arrived == "tariff" and not self._tariff_seller(device, show, on_arrival=True):
            self._release()
            return
        if arrived == "missing":
            # Seen live: the link was fired into an emulator that was still booting, went nowhere, and the
            # window said ACTIVE in that stream while the emulator showed its home screen.
            st.note(f"could not open {show.seller}'s stream on {device.name}; trying again")
            if not (st.pinned_url and show.id in st.pinned_url):
                self._ended[show.id] = now + 120        # something else first; a pinned one is simply retried
            self._release()
            return
        self._lost_reads = 0
        st.camped = show
        st.camped_since = now
        st.entered_here = 0
        st.giveaways_seen_here = 0
        self._idle_since = now
        self._card_handled = False
        self._live_checked = now            # first question LIVE_CHECK_S after arriving
        self._no_card_reads = 0
        self.store.upsert_show(show)
        self.notifier.plain(f"Camped in {show.seller} ({show.game}, {show.viewers} viewers)")

    def _tariff_seller(self, device, show: LiveShow, on_arrival: bool) -> bool:
        """'ATTENTION - additional tariffs will apply': the seller ships from another
        country. Blacklist them from the scanner for good and leave. Returns True
        only when we should stay anyway, which is a stream the user pinned by hand.
        """
        st = self.state
        if show.seller not in self.store.blacklisted_sellers():
            self.store.blacklist_add(show.seller, "tariffs")
            st.note(f"BLACKLISTED {show.seller}: additional tariffs apply")
            self.notifier.plain(f"Blacklisted {show.seller} from the scanner: additional tariffs apply")
        if st.mode == "single" and st.pinned_url and show.id in st.pinned_url:
            if on_arrival and hasattr(device, "join_past_notice"):
                device.join_past_notice()            # you chose this stream; go in
            return True
        # Measured on the emulator: BACK does not close the interstitial, and it
        # does not need closing - the next deep link lands over it in ~6s.
        st.camped = None                             # next tick picks somewhere else
        return False

    HOME_ALIASES = {"US": {"US", "USA", "UNITED STATES"}, "UK": {"UK", "GB", "UNITED KINGDOM"},
                    "CA": {"CA", "CAN", "CANADA"}, "AU": {"AU", "AUS", "AUSTRALIA"}}

    def _foreign_only(self, show: LiveShow, region: str) -> bool:
        """The card says '<region> Only Giveaway'. If that is not our country the giveaways here cannot
        be won: blacklist the seller for good and leave, pinned or not. Returns True when we left."""
        home = (self.cfg.whatnot.home_country or "US").upper()
        if not region or region in self.HOME_ALIASES.get(home, {home}):
            return False
        st = self.state
        if show.seller not in self.store.blacklisted_sellers():
            self.store.blacklist_add(show.seller, f"{region} only giveaways")
            st.note(f"BLACKLISTED {show.seller}: {region} only giveaways")
            self.notifier.plain(f"Blacklisted {show.seller} from the scanner: {region} only giveaways")
        self._unpin(show)
        st.camped = None                             # next tick picks somewhere else
        return True

    def _watch_here(self, device, now: float):
        """We are already in the stream. Look at the screen and act.

        `camped` is snapshotted for the whole tick: the UI thread clears it when
        you pin a different stream, and dereferencing it mid-entry threw and took
        the engine thread down with it.
        """
        st = self.state
        show = st.camped
        if show is None:
            return
        try:
            if hasattr(device, "snapshot"):
                ns, pkg = device.snapshot()
                if pkg and pkg.endswith("systemui"):
                    # the shade drifted over the app: clear it rather than sit here
                    # forever thinking you are using the phone
                    device._clear_system_ui()
                    ns, pkg = device.snapshot()
                if pkg and device.is_user_busy(pkg):
                    st.last_action = "waiting: you are using the phone"
                    return
                s, enter_xy, _ = device._screen_from(ns)
            else:                                   # simple devices in tests
                ns, enter_xy = None, None
                s = device.giveaway_state()
        except Exception as e:
            self._error(device, "read", e)
            return

        if getattr(s, "tariff_notice", False) and not self._tariff_seller(device, show, on_arrival=False):
            return
        if self._foreign_only(show, getattr(s, "region_only", "")):
            return

        # Are we still in the stream at all? Whatnot crashed, the emulator restarted, someone pressed Home.
        foreground = pkg if hasattr(device, "snapshot") else ""
        lost = (bool(foreground) and foreground != self.cfg.device.whatnot_package) or not getattr(s, "in_stream", True)
        if lost and not s.login_needed:
            self._lost_reads += 1
            if self._lost_reads >= 3:                   # one odd read (an animation, a dialog) is not enough
                self._lost_reads = 0
                st.note(f"{device.name} is not in {show.seller}'s stream any more; reopening it")
                try:
                    arrived = device.open_show(show.url)
                except Exception as e:
                    self._error(device, "open_show", e)
                    arrived = "missing"
                if arrived == "missing":
                    st.note(f"could not open {show.seller}'s stream on {device.name}; trying again")
                    st.camped = None
                return
        else:
            self._lost_reads = 0

        if getattr(s, "overlay", False):
            # A sheet is covering the stream (seen live: Options / Report / Sound,
            # opened by a stale badge tap landing on 'More'). Everything below
            # would misread it as 'no giveaway running'. Dismiss and re-read next tick.
            if hasattr(device, "dismiss_overlay"):
                device.dismiss_overlay()
            st.note(f"dismissed an overlay covering {show.seller}")
            return

        if s.login_needed:
            st.paused, st.paused_reason = True, "login"
            self._login_checked = now
            self._release()
            path = self._shot(device, "login")
            st.note(f"Whatnot wants a sign-in on {device.name}; waiting for you (it resumes by itself)")
            self.notifier.alert(f"Whatnot wants a sign-in on {device.name}. Paused until you sign in; "
                                f"it resumes by itself.", path)
            return

        if s.won:
            # The dialog stays up for many reads. This used to alert on every one:
            # twenty @-mentions in 75 seconds for a single win.
            self._not_won_reads = 0
            if not self._win_announced:
                self._win_announced = True
                # The winner dialog does not name the prize (seen live: 'jswike won the giveaway!'), so say
                # what we last entered here.
                prize = s.prize or getattr(self, "_last_entered", {}).get(show.id, "")
                self.store.record_win("", show, prize)
                path = self._shot(device, "win")
                self.notifier.alert(f"WON in {show.seller}! {prize} {show.url}", path)
                st.note("WIN detected")
        else:
            self._not_won_reads += 1
            if self._not_won_reads >= 2:
                self._win_announced = False

        if not s.card_visible:
            # A giveaway ends when its card goes away, which is what lets the
            # next card count as new. But a single blank read also happens
            # mid-animation, and clearing on that re-entered live giveaways.
            # Require two in a row.
            self._no_card_reads += 1
            if self._no_card_reads >= 2:
                self._card_handled = False      # next card is a fresh giveaway
            st.last_action = f"in {show.seller}, no giveaway running"
            if self._idle_since == 0.0:
                self._idle_since = now
            if not self._still_live(show, now):
                st.note(f"{show.seller}'s stream has ended; moving on")
                self._ended[show.id] = now + 900
                self._unpin(show)
                st.camped = None                 # step() picks the next one on the next tick
                return
            self._maybe_move_on(now)
            return

        self._idle_since = 0.0
        self._no_card_reads = 0
        # One attempt per card. The entry count ticks up while the card is open,
        # so anything keyed on it looks like a new giveaway every few seconds and
        # the bot re-enters the one it just entered. The card vanishing is what
        # marks the end of a giveaway, so that is what clears the flag.
        key = f"{show.id}:{int(now)}"
        if self._card_handled and self._new_giveaway_behind_the_same_badge(s, now):
            self._card_handled = False
        if s.entered:
            st.last_action = f"entered, holding in {show.seller}"
            self._card_handled = True
            return
        if self._card_handled:
            # Say WHY nothing is being tapped: an open card with an untouched Enter button looks like a fault.
            st.last_action = getattr(self, "_handled_why", "") or f"already handled this giveaway in {show.seller}"
            return
        if self.store.entries_in_last_hour(now, self.account) >= self.cfg.pacing.max_entries_per_hour:
            st.last_action = "hourly entry cap reached"
            return

        self._enter_here(device, s, now, key, show, ns)

    def _enter_here(self, device, s, now: float, key: str, show, ns=None):
        """Decide and tap with as few screen reads as possible.

        The expanded card lapses after a few seconds, so every extra dump between
        expanding and tapping is a chance to miss. `expand_then_read` taps the
        badge and returns one dump containing the prize, the entry count AND the
        button's coordinates, so the confirm gate runs and the tap lands without
        another round trip.
        """
        st = self.state
        st.giveaways_seen_here += 1
        if self.activity is not None:
            self.activity.record(show.id, now)         # how often this stream REALLY runs them (givvy.chooser)
            self.activity.note_entrants(show.id, getattr(s, "entries", None))
        t_seen = time.time()
        tapped = None                  # (screen, score, combined) at the moment we tapped Enter
        try:
            for attempt in range(MAX_EXPANDS):
                if hasattr(device, "expand_then_read"):
                    st.last_action = f"opening card ({attempt + 1})"
                    # first attempt reuses the read that spotted the card; later
                    # attempts must look again
                    s, enter_xy, combined = device.expand_then_read(ns if attempt == 0 else None)
                else:                                   # simple devices in tests
                    device.expand_card()
                    s = device.giveaway_state()
                    enter_xy, combined = (0, 0), s.follow_on_enter
                if s.prize and self.on_prize_seen is not None:
                    try:
                        self.on_prize_seen(show.id, s.prize)
                    except Exception:
                        pass
                if s.entered:
                    if tapped is not None:
                        # Seen live, on every entry: the card collapses after the
                        # Enter tap, so the verify read shows a bare badge; re-opening
                        # it shows "You're in the Giveaway". That IS our entry. It used
                        # to be logged 'already entered' and never recorded, so the
                        # hourly cap, the counts and Discord all missed it.
                        self._record_entered(device, show, key, *tapped, t_seen)
                        return
                    st.note("already entered this one")
                    self._card_handled = True
                    return
                if enter_xy is None:
                    if not s.card_visible:
                        st.note("giveaway closed before we could enter")
                        self._card_handled = True
                        return
                    log.info("card open=%s but no button: entries=%s prize=%r",
                             getattr(s, "prize", "") != "", s.entries, s.prize)
                    continue                            # still collapsed; try again
                if self._foreign_only(show, getattr(s, "region_only", "")):
                    return
                verdict = self._judge(show, s.prize)
                if verdict == "buyers":
                    # The listing can be missing or wrong; the card's own words are not. Skipped, and we stay:
                    # a buyers giveaway beside open packs does not make the stream worse (agreed 2026-09-19).
                    st.note(f"skipped {s.prize}: buyers-only giveaway")
                    self._handled_why = f"NOT entering {s.prize}: buyers only. Waiting for the next giveaway"
                    self.store.record_entry(key, show, device.name, s.entries, "skipped", account=self.account)
                    self._card_handled = True
                    return
                if verdict == "leave":
                    # We came here because every giveaway listed was a pack or better. This one is not.
                    st.note(f"{s.prize or 'a giveaway with no readable prize'} is not a pack or better: "
                            f"leaving {show.seller} for the rest of this broadcast")
                    self.store.record_entry(key, show, device.name, s.entries, "skipped", account=self.account)
                    self._ended[show.id] = now + 12 * 3600
                    self._unpin(show)
                    st.camped = None                    # step() picks the next stream on the next tick
                    return
                if verdict == "skip":
                    st.note(f"skipped {s.prize or '(unreadable prize)'}: junk (card, sticker, keychain...)")
                    self._handled_why = f"NOT entering {s.prize or 'this one'}: junk. Waiting for the next giveaway"
                    self.store.record_entry(key, show, device.name, s.entries, "skipped", account=self.account)
                    self._card_handled = True
                    return
                sc, parts = score_confirmed(_stub_event(show, now), s.prize, s.entries,
                                            self.cfg.scoring, self.followed)
                if sc < self.cfg.scoring.min_score_confirm:
                    st.note(f"skipped {s.prize or '?'} ({s.entries} entries, score {sc:.0f})")
                    self.notifier.plain(f"Skipped: {s.prize or '(unknown prize)'} — "
                                        f"{show.seller} ({s.entries} entries, score {sc:.0f})")
                    self.store.record_entry(key, show, device.name, s.entries, "skipped", account=self.account)
                    self._card_handled = True
                    return
                if tapped is not None:
                    # Enter is on screen AGAIN after we tapped it: the tap did not take.
                    log.info("Enter still showing after a tap; tapping again")
                device.tap_at(enter_xy, "Enter") if hasattr(device, "tap_at") else device.enter_giveaway()
                tapped = (s, sc, combined)
                after = (device.verify_entered() if hasattr(device, "verify_entered")
                         else device.giveaway_state())
                if not after.entered and not after.card_visible:
                    # The card vanished right after the tap. Usually that is the
                    # entry animation, but it is not confirmation. Accepting it as
                    # one produced triple entries: the card came back, a blank read
                    # had cleared the handled flag, and the bot tapped again.
                    self.store.record_entry(key, show, device.name, s.entries, "unverified", account=self.account)
                    self._card_handled = True
                    st.note(f"tapped Enter on {s.prize or '?'} but could not confirm; not retrying")
                    return
                if after.entered:
                    self._record_entered(device, show, key, s, sc, combined, t_seen)
                    return
            if tapped is not None:
                # tapped, never saw the confirmation, ran out of looks: count it, do not retry
                self.store.record_entry(key, show, device.name, tapped[0].entries, "unverified", account=self.account)
                st.note(f"tapped Enter on {tapped[0].prize or '?'} but could not confirm; not retrying")
            else:
                st.note("could not open the card in time")
                self.store.record_entry(key, show, device.name, s.entries, "missed", account=self.account)
            self._card_handled = True
        except Exception as e:
            self._error(device, "enter", e)

    def _record_entered(self, device, show, key: str, s, sc: float, combined: bool, t_seen: float):
        st = self.state
        if combined:
            self.followed.add(show.seller)
            self.store.record_follow(show.seller, self.account)
        self.store.record_entry(key, show, device.name, s.entries, "entered", account=self.account)
        st.entered_here += 1
        self._card_handled = True
        desc = ""
        if self.prize_desc_lookup is not None and s.prize:
            try:
                desc = self.prize_desc_lookup(show.id, s.prize) or ""
            except Exception:
                desc = ""
        what = (s.prize or "?") + (f" [{desc}]" if desc else "")     # a title can say 'Bookmark my stream' for a pack
        self._last_entered = {show.id: what}
        st.note(f"ENTERED {what} ({s.entries} entries) "
                f"in {time.time() - t_seen:.1f}s from seeing the card")
        self.notifier.plain(format_entry(what, show.seller, show.game, s.entries, sc))

    def _patience(self, show, now: float) -> float:
        """How long to sit with nothing happening. 90 s for a stream never seen to run a giveaway; a stream
        with a known rhythm gets one and a half of its own intervals (at most 15 min), or 'a pack every
        5 minutes' would be abandoned after 90 s of waiting for the next one."""
        p = self.cfg.pacing
        base = p.switch_after_quiet_s if self.state.giveaways_seen_here else p.switch_if_never_active_s
        gap = self.activity.interval(show.id, now) if self.activity is not None else None
        return max(base, min(900.0, 1.5 * gap)) if gap else base

    def _upgrade(self, now: float) -> bool:
        """In a fallback stream (mixed / other): move as soon as a pack-only stream is free. True = moving."""
        from . import chooser
        st = self.state
        camped = st.camped
        if camped is None or now - self._upgrade_checked < self.UPGRADE_CHECK_S:
            return False
        self._upgrade_checked = now
        if st.pinned_url and camped.id in st.pinned_url:
            return False                               # you put it here
        if self._kind(camped) in (chooser.PACK_ONLY, chooser.LEGACY):
            return False
        ranked = rank_streams(self._live_shows(), self.giveaway_counts, self.cfg, self.followed,
                              self.store.blacklisted_sellers(), self._others() | {camped.id},
                              set(self.store.favorites()), **self._rank_extras(now))
        best = next((p for p in ranked if p.kind == chooser.PACK_ONLY and p.show.id not in self.cooldowns), None)
        if best is None:
            return False
        st.note(f"a pack-only stream is available ({best.show.seller}, {best.show.viewers} viewers); "
                f"leaving {camped.seller}")
        st.camped = None
        return True

    def _maybe_move_on(self, now: float):
        """Called while no giveaway is on screen."""
        st = self.state
        if self._upgrade(now):
            return
        if st.mode == "single":
            self._leave_if_dead(now)
            return
        camped = st.camped
        if camped is None:
            return
        limit = self._patience(camped, now)
        quiet_for = now - (self._idle_since or now)
        if quiet_for < limit:
            return
        others = rank_streams(self._live_shows(), self.giveaway_counts, self.cfg, self.followed,
                              self.store.blacklisted_sellers(), self._others() | {camped.id},
                              set(self.store.favorites()), **self._rank_extras(now))
        if not others:
            self._idle_since = now                     # nowhere else to go: look again after another spell
            return
        st.note(f"quiet for {quiet_for:.0f}s; moving to {others[0].show.seller}")
        # Without this the scanner picked the same dead stream again minutes later: five times in a row, seen live.
        self.cooldowns[camped.id] = now + self.cfg.pacing.quiet_cooldown_s
        st.camped = None                               # step() will move next tick

    def _leave_if_dead(self, now: float):
        """'Stay in one stream' = stay while it actively has giveaways, otherwise find one that does.

        This mode used to never leave. Seen live: the pinned streams ended overnight, each was replaced
        by the top-ranked stream, and the bot then sat in three silent 3-viewer streams for up to 3.7 h.
        It never hops to something that merely ranks higher; that is what Scan mode is for.
        """
        st = self.state
        camped = st.camped
        if camped is None:
            return
        chosen_by_you = bool(st.pinned_url and camped.id in st.pinned_url)
        p = self.cfg.pacing
        # A stream you picked, or one that has run giveaways for us, gets real patience. One the bot
        # picked itself as a replacement and that has shown nothing gets the scanner's short one.
        limit = p.single_quiet_s if (chosen_by_you or st.giveaways_seen_here) else p.switch_if_never_active_s
        quiet_for = now - (self._idle_since or now)
        if quiet_for < limit:
            return
        ranked = rank_streams(self._live_shows(), self.giveaway_counts, self.cfg, self.followed,
                              self.store.blacklisted_sellers(), self._others() | {camped.id},
                              set(self.store.favorites()), **self._rank_extras(now))
        if not ranked:
            self._idle_since = now              # nowhere to go: look again after another spell
            return
        st.note(f"no giveaway in {camped.seller} for {quiet_for / 60:.0f} min; looking for a stream that has them")
        self.cooldowns[camped.id] = now + p.quiet_cooldown_s
        self._unpin(camped)
        st.camped = None                        # step() picks the next one on the next tick

    # ---- misc ----
    def _shot(self, device, label: str) -> str | None:
        try:
            return device.screenshot(str(Path(self.cfg.paths.screens_dir) / f"{int(time.time())}_{label}.png"))
        except Exception:
            return None

    def _error(self, device, place: str, e: Exception):
        log.exception("camper error in %s", place)
        self.store.record_error(place, str(e), self._shot(device, place))
        self.state.note(f"error in {place}: {e}")


def _stub_event(show: LiveShow, now: float):
    """score_confirmed only reads show/viewers/started_at/saw_start."""
    from .models import GiveawayEvent
    return GiveawayEvent(show=show, live_product_id="", status="active", listing=None,
                         started_at=now, viewers=show.viewers, saw_start=True)
