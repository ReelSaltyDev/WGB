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
                 listed: dict[str, int] | None = None,
                 cooling: set[str] | frozenset = frozenset()) -> list[StreamPick]:
    """Rank whole STREAMS as places to sit, not individual giveaways.

    The thing that matters most is whether the stream actually runs giveaways.
    An early version weighted odds too heavily and parked in a 4-viewer stream
    that never ran one, which is a perfect score on paper and useless in fact.
    So: streams below a viewer floor are excluded, observed giveaway activity
    dominates, and the odds term is capped so it can inform but not decide.
    """
    out: list[StreamPick] = []
    for s in shows:
        if s.id in skip_ids:
            continue                                   # another of our accounts is in it
        if s.seller in blocked:
            continue                                   # blacklisted (tariff seller)
        fav = s.seller.lower() in favorites
        if s.viewers < cfg.scoring.min_viewers_to_camp and not fav:   # you starred it: your call
            continue                                   # too small to be running giveaways
        game_w = float(cfg.scoring.game_w.get(s.game, cfg.scoring.game_w.get("other", 0)))
        runs = giveaway_counts.get(s.id, 0)
        activity = min(120.0, runs * 40.0)             # seen it run giveaways: the strongest signal
        # Small stream = better odds. The cap scales with the weight, so odds_w is a real dial:
        # at 1.0 anything under ~100 viewers gets the full 30; at 3.0 a tiny stream gets 90.
        chance = min(30.0 * cfg.scoring.odds_w, cfg.scoring.odds_w * 3000.0 / (s.viewers + 1))
        seller = float(cfg.scoring.seller_bonus) if s.seller in followed else 0.0
        star = float(cfg.scoring.favorite_bonus) if fav else 0.0
        # What the seller has LISTED (None/absent = not looked up yet, which is neutral).
        # Seen live: a 16-viewer Riftbound stream listing nothing outscored everything.
        n_listed = (listed or {}).get(s.id)
        lst = 0.0 if n_listed is None else (float(cfg.scoring.listed_bonus) if n_listed > 0
                                            else (0.0 if fav else -float(cfg.scoring.none_listed_penalty)))   # a favorite is your call
        # A stream we just left because nothing happened: not banned, but last in line.
        cold = -float(cfg.scoring.quiet_penalty) if (s.id in cooling and not fav) else 0.0
        total = round(game_w + activity + chance + seller + star + lst + cold, 2)
        out.append(StreamPick(s, total, f"game {game_w:.0f} + activity {activity:.0f} "
                                        f"({runs} seen) + odds {chance:.0f} + seller {seller:.0f}"
                                        + (f" + favorite {star:.0f}" if fav else "")
                                        + (f" + {n_listed} listed {lst:.0f}" if lst > 0 else "")
                                        + (f" - none listed {-lst:.0f}" if lst < 0 else "")
                                        + (f" - quiet there just now {-cold:.0f}" if cold else "")))
    out.sort(key=lambda p: (p.score, GAME_ORDER.get(p.show.game, 0)), reverse=True)
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
        self._card_handled = False     # have we already acted on the card on screen?
        self._no_card_reads = 0        # consecutive reads with no card (debounce)
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

    def pin(self, url: str | None):
        self.state.pinned_url = url
        if url:
            self.state.note(f"pinned to {url}")
            self.state.camped = None        # force a move to the pinned stream

    LOGIN_RECHECK_S = 20
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
        return {"listed": listed, "cooling": set(self.cooldowns)}

    def _release(self):
        if self.claims is not None:
            self.claims.release(self.account)

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
                st.camped = None
                st.last_action = "no device connected for this account"
            self._release()
            return
        if not device.is_available():
            # Undocking the phone used to leave the camper hammering a device that
            # was not there: every tick tried to navigate and logged a failure.
            st.last_action = f"{device.name} not connected - waiting"
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
        if st.pinned_url:
            for s in self.shows:
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
            cands = [s for s in self.shows if s.seller not in blocked and
                     (s.viewers >= self.cfg.scoring.min_viewers_to_camp or s.seller.lower() in favs)][:40]
            known = sum(1 for s in cands if s.id in (extras["listed"] or {}))
            if cands and known < 0.6 * len(cands):
                self._blind_since = getattr(self, "_blind_since", None) or now
                if now - self._blind_since < self.BLIND_WAIT_S:
                    st.last_action = "checking which streams have giveaways listed..."
                    return None
            else:
                self._blind_since = None
        ranked = rank_streams(self.shows, self.giveaway_counts, self.cfg, self.followed, blocked,
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
        st.camped = show
        st.camped_since = now
        st.entered_here = 0
        st.giveaways_seen_here = 0
        self._idle_since = now
        self._card_handled = False
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
                self.store.record_win("", show, s.prize or "")
                path = self._shot(device, "win")
                self.notifier.alert(f"WON in {show.seller}! {s.prize or ''} {show.url}", path)
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
            self._maybe_move_on(now)
            return

        self._idle_since = 0.0
        self._no_card_reads = 0
        # One attempt per card. The entry count ticks up while the card is open,
        # so anything keyed on it looks like a new giveaway every few seconds and
        # the bot re-enters the one it just entered. The card vanishing is what
        # marks the end of a giveaway, so that is what clears the flag.
        key = f"{show.id}:{int(now)}"
        if s.entered:
            st.last_action = f"entered, holding in {show.seller}"
            self._card_handled = True
            return
        if self._card_handled:
            st.last_action = f"already handled this giveaway in {show.seller}"
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
        st.note(f"ENTERED {s.prize or '?'} ({s.entries} entries) "
                f"in {time.time() - t_seen:.1f}s from seeing the card")
        self.notifier.plain(format_entry(s.prize, show.seller, show.game, s.entries, sc))

    def _maybe_move_on(self, now: float):
        """Scan mode only: after a quiet spell, consider a better stream."""
        st = self.state
        if st.mode == "single":
            return
        # A stream that has not run a single giveaway since we arrived gets far less
        # patience than one that simply happens to be between giveaways right now.
        limit = (self.cfg.pacing.switch_after_quiet_s if st.giveaways_seen_here
                 else self.cfg.pacing.switch_if_never_active_s)
        quiet_for = now - (self._idle_since or now)
        if quiet_for < limit:
            return
        ranked = rank_streams(self.shows, self.giveaway_counts, self.cfg, self.followed,
                              self.store.blacklisted_sellers(), self._others(), set(self.store.favorites()),
                              **self._rank_extras(now))
        if not ranked:
            return
        best = ranked[0]
        camped = st.camped
        if camped is None:
            return
        here = next((p for p in ranked if p.show.id == camped.id), None)
        if here is not None and best.score <= here.score + self.cfg.pacing.switch_margin:
            self._idle_since = now          # nothing clearly better; reset the timer
            return
        st.note(f"quiet for {quiet_for:.0f}s; better stream available ({best.show.seller})")
        if not st.giveaways_seen_here:
            # Nothing at all happened here. Without this the scanner picked the same
            # dead stream again minutes later: five times in a row, seen live.
            self.cooldowns[camped.id] = now + self.cfg.pacing.quiet_cooldown_s
        st.camped = None                     # step() will move next tick

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
