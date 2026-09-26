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

import collections
import json
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from . import hop
from .config import Config
from .metrics import count_collapsed
from .device import selectors as S
from .models import GAME_ORDER, LiveShow
from .notify import format_entry
from .scorer import odds, prize_value, score_confirmed
from .store import Store, same_prize

log = logging.getLogger(__name__)

MAX_EXPANDS = 4            # the expanded card re-collapses on its own
VOLUNTARY = ("upgrade", "hop")   # leave reasons (Visit) the account chose rather than had forced on it


class Stage:
    """Where an account is. Every change is logged ('[jgoblin22] watching -> entering: FREE PACK #3') and shown in
    the window, so what the bot was doing at any moment can be read straight off the log."""
    OFF = "off"                      # no device, paused (by you, for a sign-in, a safety pause), or another app is in
                                     # front of Whatnot on an emulator
    CHOOSING = "choosing"            # not in a stream: picking one
    JOINING = "joining"              # opening a stream
    WATCHING = "watching"            # in a stream, no giveaway on screen
    ENTERING = "entering"            # a giveaway is up: reading it, deciding, tapping
    CONFIRMING = "confirming"        # Enter was tapped and not yet proven (a banner hid it): waiting for proof
    HOLDING = "holding"              # this giveaway is dealt with (entered or skipped): waiting for it to end
    LEAVING = "leaving"              # decided to go: the next step chooses another stream


@dataclass
class Turn:
    """One giveaway, from its card appearing until it goes. Replaced WHOLE when a new giveaway starts, never
    reset field by field: this week's bugs were flags that one path reset and another did not."""
    handled: bool = False            # dealt with: entered, skipped, or tapped and waiting for proof
    handled_at: float = 0.0
    max_entries: int | None = None   # highest entry count seen while handled (a collapse = a new giveaway)
    why: str = ""                    # why nothing is being tapped, for the window
    no_fast: bool = False            # a fast tap already missed on this giveaway: read, then tap
    key: str = ""                    # the entries row this giveaway was recorded under: its peak goes there (end_turn)

    def mark_handled(self, now: float) -> None:
        if not self.handled:
            self.handled, self.handled_at, self.max_entries = True, now, None

    def reopen(self) -> None:
        """The same giveaway, to be entered again (a tap that did not take): keeps what we learned about it."""
        self.handled = False


@dataclass
class Visit:
    """One stay in one stream. Replaced whole when the account moves.

    Each stay is also one row in the visits table, for the 1.2.0 trial report (Camper._track_visit), with why it ended:
      quiet         nothing ran for the stream's patience (scan) or single_quiet_s (single mode)
      ended         the live check says the stream has ended
      dropped       it left the live stream list (and Whatnot says it is not live: pacing.check_live_before_leaving)
      held          another of our accounts took it while this one was paused
      upgrade       left a mixed or no-pack stream for a better pack-only one (voluntary)
      hop           left a crowded stream right after a draw for a clearly better one (voluntary: the crowd hop)
      not_pack      a pack-only stream ran something that is not a pack
      blacklisted   tariffs (on arrival: a zero-length row), another country's giveaways, or the Blacklist button
      pinned        you pinned another stream
      lost          the app was no longer in the stream and reopening it failed
      paused        you pressed Pause
      login         Whatnot asked for a sign-in
      breaker       the account circuit breaker paused the account
      device        the phone or emulator went away, or Whatnot is not installed on it
      moved         left with no reason recorded: should be rare, and shows in the report's reason counts
      open_failed   a stream link that did not land (a zero-length row)
      app stopped   Stop, or a crash or kill: closed at the last moment the account was seen there
    """
    idle_since: float = 0.0          # when the screen last showed no giveaway (0 = one is up)
    no_card_reads: int = 0           # consecutive reads with no card (two = the giveaway is over)
    lost_reads: int = 0              # consecutive reads that were not a stream at all
    live_checked: float = 0.0        # last 'is this seller still live?'
    upgrade_checked: float = 0.0     # last look for a pack-only stream (from a fallback one)
    upgrade_declined: str = ""       # the last pack-only stream this stay decided not to move to (said once)
    first_entrants: int | None = None  # the first entrant count read on a card in this stay (the trial report)
    missing_checked: int = -1        # the stream list (Camper._shows_seq) already asked about while this stream was
                                     # missing from it (Camper._gone): one question per list per stay
    # The crowd hop's facts (Camper._hop_drain). They belong to the stay, not the giveaway: the Turn is replaced by the
    # holding <-> watching flicker (543 flips in 4.8 h, 78% back within 10 s), so it cannot say 'our entry is open'.
    entry_at: float = 0.0            # when this account last entered here, or first saw an entry of its own here
    entry_socket_id: str | None = None   # the socket's id of the giveaway that entry is in, once known
    entry_seen_at: float = 0.0       # the last read that showed our entry ('You're in', or the tick on the badge)
    entry_drawn_at: float = 0.0      # when the draw of that entry's giveaway was seen (its 'ended', or a new giveaway on
                                     # screen), in every hop mode; 0 = not yet. The pack-only upgrade waits for it
    old_socket_ids: set = field(default_factory=set)   # ids of giveaways over for us: an earlier entry here was in one,
                                     # or it was still the socket's when we entered the next. Their 'ended' is never ours
    socket_live: tuple | None = None # (id, started_at, saw_start) of the giveaway the socket says is running here
    hop_judged_entry: float = -1.0   # the entry_at already judged at a draw
    # Live mode (Camper._hop_tick, Camper._hop_holds_entry).
    hop_armed: bool = False          # 'I would leave if the draw happened now': no new card here is entered until our
                                     # entry's draw is judged
    hop_checked: float = 0.0         # the last time that was asked (every HOP_ARM_EVERY_S while holding an entry)
    hop_tick_at: float = 0.0         # the gate's last badge-icon screenshot
    upgrade_capped: bool = False     # 'not moving to a pack-only stream yet' was said in this stay
    hop: hop.HopPlan | None = None   # a leave decided at a draw, waiting for the linger and the proof


@dataclass
class Breaker:
    """The account circuit breaker: stream links failing on several sellers point at the account, not at the streams.
    It pauses the account and alerts (Camper._trip_breaker); it never switches account, device or connection.

    The 9/22 jswike suspension made 26 failed stream opens on 9 sellers, 12:03:25-12:23:33, and nothing noticed.
    Replaying givvy.log 9/16 to the 9/24 analysis: 5 failures on 4 sellers within 10 minutes trips only at 9/22
    12:06:32 (the true case). The proposed 3-in-2 also trips falsely on jgoblin22 9/19 15:04:09, during two manual
    emulator restarts; the worst 10 minutes outside the suspension held 4 failures on 3 sellers. Later on 9/24, when
    jswike came back, its first stream links failed for 11 minutes (10 on 4 sellers by 12:37:48, when this would have
    tripped) until one opened at 12:40:57: Whatnot had signed the app out, and John signed it in by hand. A link that
    lands on the sign-in screen is a sign-in pause now (Camper._sign_in_on_open), which this never counts. The sellers
    condition also keeps a pinned stream (retried with no _ended) from tripping it on its own. Only failures that point
    at the account count (Camper._open_failed): the device still up, and Whatnot saying the stream is live."""
    failures: list = field(default_factory=list)   # (time, seller) of each failed open that counted, in the window
    retry_at: float = 0.0            # after a trip: when one stream is tried again
    wait_s: float = 0.0              # the wait before that try; doubles after each failed try
    probing: bool = False            # that one stream is being tried now

    def note(self, now: float, seller: str, s) -> bool:
        """One more failed open (`s`: SafetyCfg). True = trip."""
        self.failures = [(t, x) for t, x in self.failures if now - t < s.breaker_window_s] + [(now, seller)]
        return (len(self.failures) >= s.breaker_failures
                and len({x for _t, x in self.failures}) >= s.breaker_sellers)

    def reset(self) -> None:
        self.failures, self.retry_at, self.wait_s, self.probing = [], 0.0, 0.0, False


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
    paused_reason: str = ""            # the bot paused itself and will look again: "login" (a sign-in screen) or
                                       # "breaker" (stream links failing, see Breaker); "": you did
    device_name: str | None = None
    pinned_url: str | None = None          # single mode: the stream the user chose
    recent: list[str] = field(default_factory=list)
    tag: str = ""                          # account label, shown when several accounts run
    sink: object = None                    # callable(line): the window's merged activity log
    stage: str = "off"                     # Stage: where the account is right now

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
        from .whoami import NameLearner
        self.names = NameLearner(store, account)       # this account's real Whatnot name, from the chat (whoami.py)
        self.followed = set(followed or ()) | set(store.followed_sellers(account))
        self.state = CamperState()
        self.shows: list[LiveShow] = []
        self.giveaway_counts: dict[str, int] = {}
        self.turn = Turn()             # the giveaway on screen (see Turn)
        self.visit = Visit()           # this stay in this stream (see Visit)
        self._off = ""                 # why the account is OFF this step, if it is
        # 'Is this seller still live?' (seller, show_id) -> True / False / None = could not tell.
        # The category list is only re-read every few minutes; this is what gets us out of an
        # ended stream quickly. Also asked before leaving a stream missing from a stream list (_gone)
        # and before going back to one after a restart (_resume_target). Wired by the engine; None in most tests.
        self.live_check = None
        self.activity = None                # givvy.chooser.ActivityBook, shared by all accounts (wired by the engine)
        # (show_id, title on the card) -> True when Whatnot flags that listing as a buyers giveaway.
        # Some carry no such word ('EOS GIVY'), and the card on screen does not show the flag.
        self.buyers_title_lookup = None
        self.prize_grade_lookup = None      # (show_id, title on the card) -> 2 wanted / 1 unknown / 0 unwanted
        self.prize_desc_lookup = None       # (show_id, title on the card) -> the listing's description, if it adds anything
        self.prize_name_lookup = None       # show_id -> what a giveaway there is called, when the card was never read
        self._ended: dict[str, float] = {}     # show id -> forget after; the list may still carry it
        self._win_announced = False    # one alert per win, not one per read
        self.on_prize_seen = None      # callable(show_id, prize): the window shows it in the stream list
        self.listed_lookup = None      # callable() -> {show_id: enterable giveaways listed}
        self.cooldowns: dict[str, float] = {}   # show_id -> until; shared between accounts by the engine
        self._not_won_reads = 0
        # The visits row of this stay (_track_visit). A leave sets _left_why (a Visit reason) on the line before it
        # clears state.camped; _move_to sets _just_opened when a stream link put us here.
        self._visit_id: int | None = None
        self._visit_show = ""
        self._visit_touched = 0.0
        self._left_why = ""
        self._just_opened = ""
        self.breaker = Breaker()       # the account circuit breaker (see Breaker)
        self.alerts = None             # givvy.alerts.Alerts, shared by all accounts (wired by the engine); None = notifier
        # Where to go back to after a restart: (LiveShow, when last seen there). Set when the emulator goes away, or
        # from state.db after an app restart (load_saved_stream); used once, by the next _pick_stream.
        self._resume: tuple | None = None
        self._where_saved_at = 0.0     # the last time _save_where wrote this account's stream to state.db
        self._where_blank = False      # _save_where last wrote '' (nothing to go back to)
        self._shows_seq = 0            # which stream list this is: one question per list for a missing stream (_gone)
        self._shows_complete = True    # False: only the category feeds so far (a start); never grounds for leaving
        # The socket's news about the stream we are in, for the crowd hop: (status 'active' / 'ended', show_id,
        # live_product_id, when it came, started_at, saw_start). Appended by Engine._giveaway_seen on the socket thread,
        # taken by _hop_drain on this one.
        self.socket_events: collections.deque = collections.deque(maxlen=20)
        Path(cfg.paths.screens_dir).mkdir(parents=True, exist_ok=True)

    # ---- inputs ----
    def set_shows(self, shows: list[LiveShow], complete: bool = True):
        """`complete` False: the category feeds only, before the followed sellers are looked up (a start). Streams
        found only through those lookups are missing from it, so it is never a reason to leave one."""
        # This runs on the discovery thread: False before the list, True only after it, so a step reading in between
        # never takes the category feeds alone for a full list.
        if not complete:
            self._shows_complete = False
        self.shows = shows
        self._shows_complete = complete
        self._shows_seq += 1

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
        self._unpin(show, "blacklisted by hand")                   # single mode would walk straight back into a pinned one
        self._left_why = "blacklisted"
        self.state.camped = None            # step() picks the next stream on the next tick
        return show.seller

    def _unpin(self, show, why: str = "left the stream") -> None:
        """We are leaving `show` for good. A pin on a stream that ended or died would only drag us back to it.

        Every way of leaving goes through here, so this is also where a 'Stay in one stream' stay ends: John's rule
        (2026-09-25) is stay until the stream ends or goes 10 min without a giveaway, then back to Scan streams.
        The window's mode box follows state.mode on its next tick and saves it."""
        if self.state.pinned_url and show.id in self.state.pinned_url:
            self.state.pinned_url = None
        if self.state.mode == "single":
            self.state.mode = "scan"
            self.state.note(f"stay in {show.seller} is over ({why}): back to Scan streams")

    def pin(self, url: str | None):
        self.state.pinned_url = url
        if url:
            self._resume = None             # a stream you pick by hand beats a saved one (resume is tried before pins)
            self.state.note(f"pinned to {url}")
            self._left_why = "pinned"
            self.state.camped = None        # force a move to the pinned stream

    LOGIN_RECHECK_S = 20
    LIVE_CHECK_S = 20      # while idle in a stream: how often to ask whether it is still live
    BLIND_WAIT_S = 30      # how long the scanner waits for giveaway lists before choosing without them

    def pause(self, yes: bool):
        self.state.paused = yes
        self.state.paused_reason = ""               # pressed by you: stays until you press Resume
        if yes:
            self._release()
        else:
            # Resume is the manual override: a safety pause or a sign-in pause is over because you said so. Their
            # alerts go with it (silently), or a stale one would hold back the out-of-stream alert for good.
            self._end_breaker()
            if self.alerts is not None:
                self.alerts.resolve("breaker:" + self.account)
                self.alerts.resolve("login:" + self.account)
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
        if not ns:
            # An empty read says nothing. The only two in givvy.log 9/16-9/24 came on main at 12:28:52 and 12:29:04 on
            # 9/24, as Whatnot started there at the start of the sign-in incident (see _sign_in_on_open). Taken for
            # 'signed in', the next stream link would land on the sign-in screen and alert again.
            return
        if s.login_needed:
            st.last_action = f"waiting for you to sign in to Whatnot on {device.name}"
            return
        st.paused, st.paused_reason = False, ""
        st.camped = None                             # start clean: pick a stream and open it
        st.note("signed in; resuming")
        if self.alerts is not None:
            self.alerts.resolve("login:" + self.account, f"Signed in on {device.name}; resuming")
        else:
            self.notifier.plain(f"Signed in on {device.name}; resuming")

    def _login_pause(self, device, now: float) -> None:
        """Whatnot's sign-in screen, read in a stream (_watch_here) or where a stream link landed (_sign_in_on_open):
        pause this account ('login'), release its stream, alert once, and look again every LOGIN_RECHECK_S
        (_recheck_login), which resumes it by itself once the app is signed in."""
        st = self.state
        st.paused, st.paused_reason = True, "login"
        self._login_checked = now
        self._release()
        path = self._shot(device, "login")
        st.note(f"Whatnot wants a sign-in on {device.name}; waiting for you (it resumes by itself)")
        text = f"Whatnot wants a sign-in on {device.name}. Paused until you sign in; it resumes by itself."
        if self.alerts is not None:
            self.alerts.fire("login:" + self.account, text, self.account, path)
        else:
            self.notifier.alert(text, path)

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

    def end_turn(self) -> None:
        """The giveaway on screen is over for us: the card went, a new one started behind the badge, we moved, or the
        bot is stopping. Its highest entrant count goes on its row: the near-final crowd, a median 2.27x the count
        at entry (n=108, measured 2026-09-24). Only on the row: the ActivityBook's crowd model is calibrated on the
        count at entry. A Turn ends only here or through _card_handled = False, and a move goes through _move_to."""
        if self.turn.key and self.turn.max_entries is not None:
            try:
                self.store.note_peak(self.turn.key, self.turn.max_entries)
            except Exception:
                log.exception("could not save the peak entrant count")

    def end_stay(self, reason: str = "app stopped", now: float | None = None) -> None:
        """Stop, or an update restart: the stay's row ends now. Left open, Start in the same session would carry on
        the same row and count the stopped time as time in the stream. After Start a new row opens (opened 0: no
        stream link is fired)."""
        now = time.time() if now is None else now
        self._hop_drop(now)
        if self._visit_id is None:
            return
        try:
            self._close_stay(now, reason)
        except Exception:
            log.exception("could not close the stay")

    @property
    def _card_handled(self) -> bool:
        return self.turn.handled

    @_card_handled.setter
    def _card_handled(self, value: bool) -> None:
        if value:
            self.turn.mark_handled(getattr(self, "_now", None) or time.time())
        else:
            self.end_turn()
            self.turn = Turn()                 # a new giveaway: every per-giveaway flag starts afresh, together

    @property
    def _idle_since(self) -> float:
        return self.visit.idle_since

    @_idle_since.setter
    def _idle_since(self, value: float) -> None:
        self.visit.idle_since = value

    # ---- stages ----
    def _to(self, stage: str, why: str = "") -> None:
        st = self.state
        if stage != st.stage:
            log.info("[%s] %s -> %s%s", self.account, st.stage, stage, f": {why[:140]}" if why else "")
            st.stage = stage

    def _settle_stage(self, before) -> None:
        """Where the account ended up after a step."""
        st = self.state
        if self._off:
            self._to(Stage.OFF, self._off)
        elif st.camped is None:
            self._to(Stage.LEAVING if before is not None else Stage.CHOOSING, st.last_action)
        elif (getattr(self, "_pending", None) or {}).get("show") == st.camped.id:
            self._to(Stage.CONFIRMING, st.last_action)
        elif self.turn.handled:
            self._to(Stage.HOLDING, st.last_action)
        else:
            self._to(Stage.WATCHING, st.last_action)

    def _new_giveaway_behind_the_same_badge(self, s, now: float) -> bool:
        """Seen live (skytreerips, giveaways back to back every five minutes): the badge never leaves the
        screen, so 'the card went away' never happens and #30 to #35 were taken for #29. Within one
        giveaway the entry count climbs (bar small dips); when it COLLAPSES, a new one has started. Nothing stays
        'handled' beyond hold_timeout_s: look again (an entry we already have just reads 'already entered')."""
        if s.entries is not None:
            # Measured live the day this shipped: the count also DIPS by one or two inside a giveaway
            # (26 -> 25, 23 -> 21; eleven false alarms in ten minutes). A new giveaway starts again near
            # zero, so it must at least halve and fall by three (metrics.count_collapsed).
            if count_collapsed(self.turn.max_entries, s.entries):
                log.info("[%s] entry count fell %s -> %s: a new giveaway", self.account, self.turn.max_entries, s.entries)
                return True
            self.turn.max_entries = s.entries if self.turn.max_entries is None else max(self.turn.max_entries, s.entries)
        return now - self.turn.handled_at > self.cfg.pacing.hold_timeout_s

    UPGRADE_CHECK_S = 30       # in a fallback stream: how often to look for a pack-only one
    WHERE_SAVE_EVERY_S = 60    # while in a stream: how often its row in state.db is brought up to date (_save_where)
    WHERE_KEY = "camped:"      # + the account: the kv row naming the stream it is in

    def _remember_for_resume(self, now: float) -> None:
        """The emulator has gone away (Restart pressed because the stream got laggy, a crash, a reboot).
        Remember where we were: otherwise the account picks again, and not always the same stream. The LiveShow
        itself, so the account can go back before the stream list has it."""
        if self.state.camped is not None:
            self._resume = (self.state.camped, now)

    def load_saved_stream(self) -> None:
        """After an app restart: the stream this account was in, as the last run saved it (_save_where), for the
        first pick to go back to (_resume_target). 9/23-24: 9 app starts in 4.2 h; 11 of 16 account-restarts were
        holding an entry and 10 of 16 landed on another seller. After the 9/24 06:18:25 restart saltygoblin, holding
        ducknwin #30, went to 777collect, a stream it had left at 06:15:10 for being quiet, and left it again at
        06:21:09 ('quiet for 91s'): the entry was forfeited. Nothing saved, or '' (it left on purpose): nothing to go
        back to."""
        raw = ""
        try:
            raw = self.store.kv_get(self.WHERE_KEY + self.account) or ""
            if not raw:
                return
            data = json.loads(raw)
            at = float(data.pop("at"))
            names = {f.name for f in fields(LiveShow)}
            show = LiveShow(**{k: v for k, v in data.items() if k in names})
        except Exception as e:                       # bad JSON, or a row this LiveShow cannot be built from
            log.warning("[%s] the saved stream could not be read (%s): %.120s", self.account, e, raw)
            return
        self._resume = (show, at)

    BREAKER_KEY = "breaker:"   # + the account: the kv row of a safety pause in progress (_trip_breaker, load_breaker)

    def load_breaker(self) -> None:
        """After an app restart: a safety pause the last run was in goes on (see Breaker), with the same next try. It
        lived in memory only, so every restart during one (59 app restarts in 7.3 days to 9/24) ended it, the new run
        fired stream links at once, and it took 5 more failed links to trip again, with a new alert. Its saved stream is
        not rejoined, and its alert is back in the banner without a second notification. Nothing saved, or the breaker
        switched off: nothing to carry over."""
        raw = ""
        try:
            raw = self.store.kv_get(self.BREAKER_KEY + self.account) or ""
            if not raw or not self.cfg.safety.breaker:
                return
            data = json.loads(raw)
            retry_at, wait_s = float(data["retry_at"]), float(data["wait_s"])
        except Exception as e:                       # bad JSON, or a row without the times
            log.warning("[%s] the saved safety pause could not be read (%s): %.120s", self.account, e, raw)
            return
        b, st = self.breaker, self.state
        b.retry_at, b.wait_s = retry_at, wait_s
        st.paused, st.paused_reason = True, "breaker"
        self._resume = None                          # one stream is tried at retry_at, chosen afresh
        st.note(f"SAFETY PAUSE carried over from before the restart: one stream will be tried at "
                f"{time.strftime('%H:%M', time.localtime(retry_at))}")
        if self.alerts is not None and data.get("text"):
            self.alerts.restore("breaker:" + self.account, str(data["text"]), self.account,
                                float(data.get("at") or time.time()))

    def _end_breaker(self) -> None:
        """The safety pause is over or never began (a stream opened, Resume, a sign-in pause took over): in memory and in
        state.db."""
        self.breaker.reset()
        try:
            self.store.kv_set(self.BREAKER_KEY + self.account, "")
        except Exception:
            log.exception("could not clear the saved safety pause")

    def _save_where(self, before) -> None:
        """After every step: this account's stream in state.db (kv 'camped:<account>', read back by load_saved_stream).
        'at' is the last time it was seen there, brought up to date every WHERE_SAVE_EVERY_S, so a 2-hour stay still
        resumes. A stream left on purpose (ended, quiet, an upgrade, a pack-only leave, a blacklist, a stream the app
        lost and could not reopen, held by another account, a move whose stream link failed) is saved as '': nothing
        to go back to; so is the stream of a sign-in or safety pause, which never goes back to it (_recheck_login and
        the breaker's one try pick afresh). Kept, a restart during the pause rejoined it, and a sign-in pause, which
        keeps state.camped, refreshed its 'at' every minute while out of it: after a restart it could take the stream
        from the account that had moved in. Out of the stream because the account is OFF for anything else (no device,
        paused by you, not installed, another app in front) keeps the row as it was, 'at' included: that is an emulator
        restart, which should resume, but only within resume_within_s of the last time it was really there.
        `before`: the stream at the start of the step."""
        if not self.cfg.pacing.resume_after_restart:
            return
        now = getattr(self, "_now", None) or time.time()
        st, key = self.state, self.WHERE_KEY + self.account
        if st.paused and st.paused_reason in ("login", "breaker"):
            if not self._where_blank:
                self.store.kv_set(key, "")
                self._where_blank = True
            return
        if self._off:
            return
        if st.camped is not None:
            if (self._where_blank or before is None or before.id != st.camped.id
                    or now - self._where_saved_at >= self.WHERE_SAVE_EVERY_S):
                self.store.kv_set(key, json.dumps({**asdict(st.camped), "at": now}))
                self._where_saved_at, self._where_blank = now, False
        elif before is not None:
            self.store.kv_set(key, "")
            self._where_blank = True

    def _resume_target(self, resume, shows: list[LiveShow], blocked) -> LiveShow | None:
        """The stream a restart (of the app or of the emulator) goes back to, or None: choose afresh. `resume` is
        (the saved LiveShow, when last seen there). The caller moves through _move_to, so the stream link is always
        fired again: an entry survives a rejoin (9/23 tradersshoprips #7: entered 22:20:41, rejoined 22:24:42
        'already entered', won 22:25:22), and nothing on screen says which stream the emulator is in, so adopting one
        on assumption could put two of our accounts in one stream. Refused: seen there more than resume_within_s ago,
        a blacklisted seller, a stream seen to end, one another of our accounts holds. A stream missing from the list
        (right after an app start there is none for about 80 s) goes only when Whatnot says it is live; no answer
        counts as no: 0 of its live checks failed in the whole of givvy.log to 9/24."""
        saved, at = resume
        now = getattr(self, "_now", None) or time.time()
        p, age = self.cfg.pacing, now - at
        back = next((s for s in shows if s.id == saved.id), None)     # the list's entry has fresh viewers
        how, why = "in the stream list", ""
        if not 0 <= age <= p.resume_within_s:
            why = f"left {age:.0f}s ago"
        elif saved.seller in blocked:
            why = "blacklisted"
        elif self._ended.get(saved.id, 0) > now:
            why = "seen to end"
        elif back is None:
            if not p.resume_after_restart or self.live_check is None:
                why = "not in the stream list"
            else:
                try:
                    answer = self.live_check(saved.seller, saved.id)
                except Exception as e:
                    log.warning("live check for %s failed: %s", saved.seller, e)
                    answer = None
                if answer is True:
                    back, how = saved, "Whatnot says still live"
                elif answer is False:
                    why = "Whatnot says it is not live"
                    self._ended[saved.id] = now + 900        # nor from a stale stream list
                else:
                    why = "no answer from Whatnot"
        if not why and not self._claim(back):
            why = f"held by {self.claims.holder(back.id)}"
        if why:
            self.state.note(f"not going back to {saved.seller} after a restart: {why}")
            return None
        self.state.note(f"back after a restart: rejoining {back.seller} (left {age:.0f}s ago; {how})")
        return back

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
            # It was listed as packs only, and this is not one. But only on a prize we could actually READ:
            # a half-drawn card or a banner over it must never cost us the stream for a whole broadcast.
            # 'Stay in one stream' is left only when the stream ends or goes quiet (_leave_if_dead): skip.
            if self.state.mode == "single":
                return "skip"
            return "leave" if (prize or "").strip() else "skip"
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
        if self.live_check is None or now - self.visit.live_checked < self.LIVE_CHECK_S:
            return True
        self.visit.live_checked = now
        try:
            answer = self.live_check(show.seller, show.id)
        except Exception as e:                      # no answer is not 'ended'
            log.warning("live check for %s failed: %s", show.seller, e)
            return True
        return answer is not False

    def _asks_before_leaving(self) -> bool:
        return self.cfg.pacing.check_live_before_leaving and self.live_check is not None

    def _gone(self, show, now: float) -> bool:
        """The stream we are in is missing from a full stream list: leave it? One miss used to be enough. givvy.log
        9/16 - 9/24 10:38 has 129 'is no longer live' exits; 24 of 128 were provably false (the same show camped again
        later; 8 since 9/19, about 1.6 a day), 4 by a stricter count. The misses come from the feeds paging through a
        list sorted by live viewers: not one 'feed failed' line was logged, and not one 'live check ... failed'. So
        Whatnot is asked, at most once per list in a stay, and the account leaves only when it says 'not live'. No
        answer is not 'ended', as in _still_live."""
        if not self._asks_before_leaving():
            return True                              # 1.1.x: the first miss
        if self.visit.missing_checked == self._shows_seq:
            return False                             # this list was already asked about
        self.visit.missing_checked = self._shows_seq
        self.visit.live_checked = now                # one question serves the idle check too
        try:
            answer = self.live_check(show.seller, show.id)
        except Exception as e:
            log.warning("live check for %s failed: %s", show.seller, e)
            return False
        if answer is False:
            self._ended[show.id] = now + 900         # a stale list must not walk us straight back in
            return True
        if answer is None:
            log.info("[%s] %s dropped out of the stream list; Whatnot could not tell, staying", self.account, show.seller)
            return False
        self.state.note(f"{show.seller} dropped out of the stream list but Whatnot says it is still live; staying")
        return False

    def _others(self) -> set[str]:
        return self.claims.held_by_others(self.account) if self.claims is not None else set()

    def _busy(self, device, what: str) -> None:
        """Something other than Whatnot is in front (`what`: its package, when known). On a phone that is you using it:
        wait quietly, as always. On an emulator it is usually an Android 'not responding' or system dialog, which used
        to stall the account with no log line and no stage change. There it is OFF now, saying what is in front: the
        stage change is logged, and the out-of-stream alert fires if it lasts."""
        if hasattr(device, "power_off"):
            self._off = self.state.last_action = f"{what} is in front of Whatnot"
        else:
            self.state.last_action = "waiting: you are using the phone"

    # ---- the loop ----
    def step(self, now: float | None = None):
        before = self.state.camped
        self._off = ""
        try:
            self._step(now)
        finally:
            self._settle_stage(before)
            try:
                self._track_visit()
            except Exception:
                log.exception("could not record the stay in the visits table")
            try:
                self._save_where(before)
            except Exception:
                log.exception("could not save which stream the account is in")

    VISIT_SEEN_S = 60          # how often an open stay's seen_at is brought up to date

    def _track_visit(self) -> None:
        """Opens and closes the visits row of each stay (see Visit), after every step (and end_stay on Stop). One place,
        so a move made from the window (a pin, the Blacklist button) is closed at the next step too. A pause closes
        the stay ('paused'); on resume a new row opens with opened 0, as no stream link was fired. seen_at is kept up
        to date once a minute: a stay cut off by a crash is closed at the last moment the account was known there."""
        if not self.cfg.trial.record_visits:
            self._left_why = self._just_opened = ""
            return
        now = getattr(self, "_now", None) or time.time()
        st = self.state
        here = None if self._off else st.camped
        # A stream link fired at the stream we were already in (a pin on it) starts a new stay as well.
        if self._visit_id is not None and (here is None or here.id != self._visit_show or self._just_opened == here.id):
            why = self._left_why or ("login" if st.paused_reason == "login" else
                                     "breaker" if st.paused_reason == "breaker" else
                                     "paused" if st.paused else "device" if self._off else "moved")
            self._close_stay(now, why)
        if here is not None and self._visit_id is None:
            opened = self._just_opened == here.id
            # Viewers from the latest stream list: the camped LiveShow keeps those it had when first joined.
            fresh = next((s for s in self.shows if s.id == here.id), here)
            self._visit_id = self.store.start_visit(self.account, fresh, st.camped_since if opened else now,
                                                    kind=self._kind(here), opened=opened)
            self._visit_show, self._visit_touched = here.id, now
            if self.visit.first_entrants is not None:       # read earlier in this step, before the row existed
                self.store.visit_first_entrants(self._visit_id, self.visit.first_entrants)
        elif self._visit_id is not None and now - self._visit_touched >= self.VISIT_SEEN_S:
            self.store.visit_seen(self._visit_id, now)
            self._visit_touched = now
        # A code describes only the leave in this step, or the window action just before it.
        self._left_why = self._just_opened = ""

    def _close_stay(self, now: float, why: str) -> None:
        self.store.end_visit(self._visit_id, now, why)
        self._visit_id, self._visit_show = None, ""
        # A pause keeps this Visit: the stay resumed in it reads its own first count.
        self.visit.first_entrants = None

    def _step(self, now: float | None = None):
        now = time.time() if now is None else now
        self._now = now
        st = self.state
        device = self.device_getter()
        st.device_name = device.name if device else None
        if st.paused and st.paused_reason == "login" and device is not None:
            self._off = "waiting for a sign-in"
            self._recheck_login(device, now)
            return
        if st.paused and st.paused_reason == "breaker":
            # The safety pause (see Breaker): nothing is opened until the re-check, then exactly one stream is tried
            # (_move_to clears the pause when it opens; _open_failed pauses again, for twice as long, when it does not).
            # A suspended account now costs one stream link per try, 20, 40, 80 then 120 min apart, instead of 26 in
            # 20 minutes.
            self._off = "safety pause"
            self._release()
            if now < self.breaker.retry_at:
                st.last_action = (f"SAFETY PAUSE: streams would not open; trying one again at "
                                  f"{time.strftime('%H:%M', time.localtime(self.breaker.retry_at))}")
                return
            st.paused, st.paused_reason = False, ""
            self.breaker.probing = True
            st.note("safety pause over: trying one stream")
            return
        if st.paused or device is None:
            self._off = "paused" if st.paused else "no device"
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
            self._off = st.last_action
            self._remember_for_resume(now)
            st.camped = None            # whatever we were in, we are not in it now
            self._release()
            return
        if hasattr(device, "app_installed") and not device.app_installed():
            st.last_action = f"{device.name}: install Whatnot and sign in on it (or it is still booting)"
            self._off = st.last_action
            st.camped = None
            self._release()
            return
        if st.camped is None and device.is_user_busy():
            # Only pay for the two dumpsys calls when we are about to navigate;
            # while camped, the screen read below answers this for free.
            self._busy(device, "another app")
            return

        # Stay put. A move only happens when camped is None, which is set on
        # startup, when the stream we are in ends, when the user pins a different
        # one, or when scan mode decides a quiet stream is worth leaving.
        camped = st.camped                       # snapshot: the UI thread can clear this
        shows = self.shows                       # and discovery can replace this
        # Missing from the stream list. Only from a full one: the category feeds alone (right after a start) lack
        # every stream found only through the followed-seller lookups.
        if (camped is not None and shows and self._shows_complete and all(x.id != camped.id for x in shows)
                and self._gone(camped, now)):
            st.note(f"{camped.seller} is no longer live" + (" (Whatnot confirms)" if self._asks_before_leaving() else ""))
            self._unpin(camped, "the stream ended")
            self._left_why = "dropped"
            st.camped = camped = None

        if camped is not None and not self._claim(camped):
            # a pause released our claim and another account moved in meanwhile
            st.note(f"{camped.seller} is now held by {self.claims.holder(camped.id)}; leaving it")
            self._left_why = "held"
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
        if self._resume is not None:                 # used once, whatever the answer
            resume, self._resume = self._resume, None
            back = self._resume_target(resume, shows, blocked)
            if back is not None:
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
        self._to(Stage.JOINING, f"{show.seller} ({show.viewers} viewers)")
        st.note(f"moving to {show.seller} ({show.game}, {show.viewers} viewers)")
        try:
            arrived = device.open_show(show.url)
        except Exception as e:
            self._error(device, "open_show", e)
            self._open_failed(device, show, now, adb_error=True)
            self._release()
            return
        if arrived == "tariff" and not self._tariff_seller(device, show, on_arrival=True):
            self._record_open(show, now, "blacklisted")    # a stream link fired all the same: joins per hour count it
            self._release()
            return
        if arrived == "missing":
            # Seen live: the link was fired into an emulator that was still booting, went nowhere, and the
            # window said ACTIVE in that stream while the emulator showed its home screen.
            if self._sign_in_on_open(device, show, now):
                return                                  # paused for a sign-in: no other stream is tried
            st.note(f"could not open {show.seller}'s stream on {device.name}; trying again")
            if not (st.pinned_url and show.id in st.pinned_url):
                self._ended[show.id] = now + 120        # something else first; a pinned one is simply retried
            self._open_failed(device, show, now)
            self._release()
            return
        # The socket's news about earlier stays (the engine sends it only for the stream we are camped in, so nothing
        # about this one can have come yet): coming back to a stream must not replay what was said last time.
        self.socket_events.clear()
        st.camped = show
        st.camped_since = now
        self._just_opened = show.id                     # this stay began with a stream link (_track_visit)
        if self.breaker.probing:                        # the one stream after a safety pause opened: back at work
            self._end_breaker()
            st.note("the stream opened: safety pause cleared")
            if self.alerts is not None:
                self.alerts.resolve("breaker:" + self.account, f"{self.account}: streams open again; back at work")
            else:
                self.notifier.plain(f"{self.account}: streams open again; back at work")
        self.names.joined(show.id, now)             # our '<username> joined' shows in the chat now
        st.entered_here = 0
        st.giveaways_seen_here = 0
        # A new stay: everything about the last stream and its last giveaway goes, together.
        self.end_turn()                                  # the giveaway we left keeps the highest count we saw
        self._hop_drop(now)                              # a crowd hop pending there still gets its row
        self.visit = Visit(idle_since=now, live_checked=now)   # first 'still live?' LIVE_CHECK_S after arriving
        self.turn = Turn()
        if self.watcher is not None:
            # A crowd-hop target was chosen for its fresh giveaway, whose 'active' came before we were camped here.
            try:
                self.visit.socket_live = self.watcher.giveaway_running(show.id)
            except Exception:
                log.exception("could not ask the socket what is running in %s", show.seller)
        self.store.upsert_show(show)
        self.notifier.plain(f"Camped in {show.seller} ({show.game}, {show.viewers} viewers)")
        rec = getattr(device, "recorder", None)
        if rec is not None:                             # what the bot knew on arrival: replay needs the same
            try:
                info = self._listed_info(show)
                rec.meta(now, show_id=show.id, seller=show.seller, viewers=show.viewers, game=show.game,
                         kind=self._kind(show), buyers=int(getattr(info, "buyers", 0) or 0),
                         followers_only=int(getattr(info, "followers_only", 0) or 0),
                         usernames=sorted(getattr(device, "usernames", ()) or ()))
            except Exception:
                pass

    def _sign_in_on_open(self, device, show: LiveShow, now: float) -> bool:
        """A stream link did not land: is Whatnot's sign-in screen why? True = it is, and the account is now paused for
        a sign-in (_login_pause): no other stream is tried, and it resumes by itself once the app is signed in. The
        failed open is still a visits 'open_failed' row (_open_failed), and the circuit breaker leaves it alone.

        2026-09-24 12:29-12:40: after jswike's suspension ended, Whatnot on main was signed out. Its screen showed only
        'Sign up for Whatnot', 'Continue with Google / Facebook / Email', 'Already have an account?', 'Log In' and the
        terms line, and only a read in a stream looked for that screen: 15 stream links on 4 sellers failed, one every
        ~45 s, and nothing was entered until John signed in by hand. The screen is the open attempt's own last read when
        the device kept one (AndroidDevice.arrival_read), otherwise one read. A read that fails, is empty or shows no
        sign-in screen is the old path: try the next stream. Not asked when adb itself failed (the link never reached
        the phone). The same check as in a stream (login_needed): a swallowed link leaves the app on its home screen,
        which lists stream titles, and none of the 695 in state.db 9/16-9/24 starts with a sign-in phrase."""
        if not self.cfg.safety.check_sign_in_on_open:
            return False
        try:
            ns = getattr(device, "arrival_read", None)
            if not ns:
                if not hasattr(device, "snapshot"):
                    return False                     # simple devices in tests
                ns, _pkg = device.snapshot()
            s, _xy, _ = device._screen_from(ns)
        except Exception as e:
            log.warning("[%s] could not look for the sign-in screen: %s", self.account, e)
            return False
        if not s.login_needed:
            return False
        self.state.note(f"could not open {show.seller}'s stream on {device.name}: "
                        f"Whatnot is showing its sign-in screen")
        self._open_failed(device, show, now, sign_in=True)
        self._login_pause(device, now)
        self._off = "waiting for a sign-in"          # OFF from this step, as in _step; the saved stream is kept
        return True

    def _open_failed(self, device, show: LiveShow, now: float, adb_error: bool = False, sign_in: bool = False) -> None:
        """A stream link that did not land. Recorded for the trial report as failed opens per hour: the 9/22 jswike
        suspension made 26 in 20 minutes, and nothing counted them. `adb_error`: the link never reached the phone.
        That is a device problem, not the account's, so the account circuit breaker must not count it.

        For the breaker a failure counts only when it points at the account: the device is still up (an emulator that
        dropped is a device problem) and Whatnot says the stream is live. False means it had ended (a stale stream
        list, not the account); no answer means Whatnot could not be asked (the network, not the account). That check
        costs one Whatnot lookup per failed open (59 failed opens 9/16-9/23).

        `sign_in`: the link landed on Whatnot's sign-in screen (_sign_in_on_open), and the sign-in pause is that
        incident's one alert. The breaker does not count it, and drops what it had counted: failures just before the
        sign-in screen showed are most likely the same incident, and must not trip it right after you sign in. A safety
        pause whose one try met the sign-in screen ends here, its alert silently: the sign-in alert says what to do."""
        self._record_open(show, now, "open_failed")
        if sign_in:
            self._end_breaker()
            if self.alerts is not None:
                self.alerts.resolve("breaker:" + self.account)
            return
        if adb_error or not self.cfg.safety.breaker:
            return
        try:
            if not device.is_available():
                return
        except Exception:
            return
        if self.live_check is not None:
            try:
                if self.live_check(show.seller, show.id) is not True:
                    return
            except Exception as e:
                log.warning("live check for %s failed: %s", show.seller, e)
                return
        # A failed try after a safety pause pauses again at once; otherwise the window decides.
        if self.breaker.probing or self.breaker.note(now, show.seller, self.cfg.safety):
            self._trip_breaker(device, now)

    def _trip_breaker(self, device, now: float) -> None:
        """Pause this account (see Breaker): release its stream, alert once with a screenshot, and try one stream
        again after safety.breaker_recheck_s, twice as long after each failed try, at most breaker_recheck_max_s.
        It only pauses: never another account, device or connection."""
        s, b, st = self.cfg.safety, self.breaker, self.state
        n, sellers = len(b.failures), list(dict.fromkeys(x for _t, x in b.failures))
        span = now - b.failures[0][0] if b.failures else 0.0
        first = not b.probing
        b.wait_s = float(s.breaker_recheck_s) if first else min(2 * b.wait_s, float(s.breaker_recheck_max_s))
        b.retry_at = now + b.wait_s
        b.failures, b.probing = [], False
        st.paused, st.paused_reason = True, "breaker"
        self._off = "safety pause"
        self._left_why = "breaker"
        st.camped = None
        self._release()
        at = time.strftime("%H:%M", time.localtime(b.retry_at))
        if not first:
            st.note(f"SAFETY PAUSE: the stream would not open either; trying one again at {at}")
            self._save_breaker(now)
            return
        shot = self._shot(device, "breaker")
        text = (f"SAFETY PAUSE: {n} streams on {len(sellers)} sellers would not open in "
                f"{max(1, round(span / 60))} min ({', '.join(sellers)}). {self.account} is paused; one stream will "
                f"be tried at {at}. If Whatnot has restricted the account, check the app on {device.name}.")
        st.note(text)
        self._save_breaker(now, text)
        try:
            self.store.record_error("breaker", text, shot)
        except Exception:
            log.exception("could not record the safety pause")
        if self.alerts is not None:
            self.alerts.fire("breaker:" + self.account, text, self.account, shot)
        else:
            self.notifier.alert(text, shot)

    def _save_breaker(self, now: float, text: str | None = None) -> None:
        """The safety pause in state.db, for load_breaker: its next try and the wait before it, and (`text`, on the trip
        itself) the alert it raised; a later failed try keeps the alert and moves the try."""
        key = self.BREAKER_KEY + self.account
        try:
            row = json.loads(self.store.kv_get(key) or "{}") if text is None else {"at": now, "text": text}
            self.store.kv_set(key, json.dumps({**row, "retry_at": self.breaker.retry_at, "wait_s": self.breaker.wait_s}))
        except Exception:
            log.exception("could not save the safety pause")

    def _record_open(self, show: LiveShow, now: float, reason: str) -> None:
        """A stream link that did not become a stay: a zero-length visits row."""
        if not self.cfg.trial.record_visits:
            return
        try:
            self.store.record_open_attempt(self.account, show, now, reason)
        except Exception:
            log.exception("could not record the stream open")

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
        if not on_arrival:
            # On arrival there is no stay here to end (_move_to records a 'blacklisted' open), and the code would
            # relabel the stay left earlier in the same step ('dropped', 'held' or a pin).
            self._left_why = "blacklisted"
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
        self._unpin(show, f"{region} only giveaways")
        self._left_why = "blacklisted"
        st.camped = None                             # next tick picks somewhere else
        return True

    def _learn_name(self, device, show, ns, now: float) -> None:
        """Win matching by the account's real Whatnot name as well as its label (givvy/whoami.py)."""
        names = getattr(device, "usernames", None)
        if names is None:
            return
        known = self.names.name()
        if known and known not in names:
            names.add(known)
        try:
            new = self.names.saw(show.id, S.joined_names(ns), now)
        except Exception:
            log.exception("[%s] learning the Whatnot name", self.account)
            return
        if new:
            names.add(new)
            if new not in {self.account.lower(), device.name.lower()}:
                self.state.note(f"this account's Whatnot name is '{new}' (its label is {self.account}); "
                                f"its wins are recognised by both")

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
            self._hop_drain(show, now)
        except Exception:                           # the crowd hop must never stand in the way of entering
            log.exception("[%s] crowd hop", self.account)
        try:
            if hasattr(device, "snapshot"):
                ns, pkg = device.snapshot()
                if pkg and pkg.endswith("systemui"):
                    # the shade drifted over the app: clear it rather than sit here
                    # forever thinking you are using the phone
                    device._clear_system_ui()
                    ns, pkg = device.snapshot()
                if pkg and device.is_user_busy(pkg):
                    self._busy(device, pkg)
                    return
                self._learn_name(device, show, ns, now)   # before _screen_from: it matches wins by these names
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
            self.visit.lost_reads += 1
            if self.visit.lost_reads >= 3:                   # one odd read (an animation, a dialog) is not enough
                self.visit.lost_reads = 0
                st.note(f"{device.name} is not in {show.seller}'s stream any more; reopening it")
                raised = False
                try:
                    arrived = device.open_show(show.url)
                except Exception as e:
                    self._error(device, "open_show", e)
                    arrived, raised = "missing", True
                if arrived == "missing":
                    if not raised and self._sign_in_on_open(device, show, now):
                        self._left_why = "login"         # this stay ended for a sign-in
                        st.camped = None
                        return
                    st.note(f"could not open {show.seller}'s stream on {device.name}; trying again")
                    self._left_why = "lost"              # first, so a breaker trip in _open_failed can say 'breaker'
                    self._open_failed(device, show, now, adb_error=raised)
                    st.camped = None
                return
        else:
            self.visit.lost_reads = 0

        if getattr(s, "overlay", False):
            # A sheet is covering the stream (seen live: Options / Report / Sound,
            # opened by a stale badge tap landing on 'More'). Everything below
            # would misread it as 'no giveaway running'. Dismiss and re-read next tick.
            if hasattr(device, "dismiss_overlay"):
                device.dismiss_overlay()
            st.note(f"dismissed an overlay covering {show.seller}")
            return

        if s.login_needed:
            self._login_pause(device, now)
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
                self.store.record_win("", show, prize, account=self.account)
                path = self._shot(device, "win")
                self.notifier.alert(f"WON in {show.seller}! {prize} {show.url}", path)
                st.note("WIN detected")
        else:
            self._not_won_reads += 1
            if self._not_won_reads >= 2:
                self._win_announced = False

        try:
            if self._hop_tick(device, show, s, ns, enter_xy, now):
                return                                  # the crowd hop has just moved the account
        except Exception:                               # the crowd hop must never stand in the way of entering
            log.exception("[%s] crowd hop", self.account)
            if st.camped is not show:
                return                                  # it failed mid-move: this read is of the stream we left

        if not s.card_visible:
            # A giveaway ends when its card goes away, which is what lets the
            # next card count as new. But a single blank read also happens
            # mid-animation, and clearing on that re-entered live giveaways.
            # Require two in a row.
            self.visit.no_card_reads += 1
            if self.visit.no_card_reads >= 2:
                self._card_handled = False      # the card is gone: the next one is a fresh giveaway (new Turn)
            st.last_action = f"in {show.seller}, no giveaway running"
            if self._idle_since == 0.0:
                self._idle_since = now
            if not self._still_live(show, now):
                st.note(f"{show.seller}'s stream has ended; moving on")
                self._ended[show.id] = now + 900
                self._unpin(show, "the stream ended")
                self._left_why = "ended"
                st.camped = None                 # step() picks the next one on the next tick
                return
            self._maybe_move_on(now)
            return

        self._idle_since = 0.0
        self.visit.no_card_reads = 0
        # One attempt per card. The entry count ticks up while the card is open,
        # so anything keyed on it looks like a new giveaway every few seconds and
        # the bot re-enters the one it just entered. The card vanishing is what
        # marks the end of a giveaway, so that is what clears the flag.
        key = f"{show.id}:{int(now)}"
        if self._card_handled and self._new_giveaway_behind_the_same_badge(s, now):
            self._card_handled = False
        if s.entered:
            self._confirm_late(device, show, s, now)    # a tap the banner hid from us, now proven
            st.last_action = f"entered, holding in {show.seller}"
            self._card_handled = True
            self._hop_saw_entry(now)
            if not self.turn.key:
                # Back after the badge left the screen ('watching -> holding: entered', 943 times 9/23 22:21 - 9/24
                # 10:41): the row we entered it under takes the rest of its peak, and its title if never read.
                row = self.store.last_row(self.account, show.id, now - self.cfg.pacing.same_giveaway_s)
                if row is not None and row.result in ("entered", "unverified"):
                    self.turn.key = row.key
                    self.store.fill_prize(row.key, s.prize)
            return
        if self._card_handled and getattr(self, "_pending", None):
            self._ask_the_tick(device, show, ns, s, enter_xy, now)
        if self._card_handled and self._tap_did_not_take(show, s, enter_xy, now):
            self.turn.reopen()                          # fall through and enter it properly, from this read
        if self._card_handled:
            # Say WHY nothing is being tapped: an open card with an untouched Enter button looks like a fault.
            st.last_action = self.turn.why or f"already handled this giveaway in {show.seller}"
            return
        try:
            held = self._hop_holds_entry(device, show, s, ns, enter_xy, now)
        except Exception:
            log.exception("[%s] crowd hop", self.account)
            held = False
        if held:
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
        self._to(Stage.ENTERING, s.prize or "a giveaway")
        seen = getattr(s, "entries", None)             # the count on the read that spotted the card
        again = self._last_here(show, s, now)          # our last row here, when this card may still be that giveaway
        if again is None:
            # A flicker is not a new start, and its later, higher count must not reach the crowd model (calibrated on
            # the count at entry), nor be taken for the stay's first read.
            self._count_start(show, seen, now)
        t_seen = time.time()
        tapped = None                  # (screen, score, combined) at the moment we tapped Enter
        fast = False
        try:
            if (ns is not None and again is None and hasattr(device, "fast_enter")
                    and self._fast_ok(show, s, now)):
                # Badge, then Enter where it WILL be, with no read in between (see AndroidDevice.fast_enter).
                st.last_action = "entering (fast)"
                fast = bool(device.fast_enter(ns))
                if fast:
                    tapped = (s, 0.0, self._follow_on_enter(show))
            for attempt in range(MAX_EXPANDS):
                if hasattr(device, "expand_then_read"):
                    st.last_action = f"opening card ({attempt + 1})" if not fast else "confirming the entry"
                    # first attempt reuses the read that spotted the card; later
                    # attempts (and any look after a fast enter) must look again
                    s, enter_xy, combined = device.expand_then_read(ns if (attempt == 0 and not fast) else None)
                else:                                   # simple devices in tests
                    device.expand_card()
                    s = device.giveaway_state()
                    enter_xy, combined = (0, 0), s.follow_on_enter
                if s.prize and self.on_prize_seen is not None:
                    try:
                        self.on_prize_seen(show.id, s.prize)
                    except Exception:
                        pass
                if again is not None and self._another_giveaway(again, s, enter_xy):
                    again = None                        # read, and it is a new giveaway after all: count it now
                    self._count_start(show, seen, now)
                if s.entered:
                    if tapped is not None:
                        # Seen live, on every entry: the card collapses after the
                        # Enter tap, so the verify read shows a bare badge; re-opening
                        # it shows "You're in the Giveaway". That IS our entry. It used
                        # to be logged 'already entered' and never recorded, so the
                        # hourly cap, the counts and Discord all missed it.
                        rec = tapped if tapped[0].prize else (s, tapped[1], tapped[2])
                        self._record_entered(device, show, key, *rec, t_seen)
                        return
                    if self._confirm_late(device, show, s, now):
                        return
                    if again is not None:               # the giveaway our last row is about: it takes the peak
                        self.turn.key = again.key
                        self.store.fill_prize(again.key, s.prize)
                    st.note("already entered this one")
                    self._card_handled = True
                    self._hop_saw_entry(now)
                    return
                if enter_xy is None:
                    if not s.card_visible:
                        if tapped is not None:          # we DID tap; something (a break-spot banner) is over the card
                            self._unverified(device, show, key, tapped, s, now, t_seen)
                            return
                        st.note("giveaway closed before we could enter")
                        self._card_handled = True
                        return
                    if (self.cfg.pacing.skip_buyers_without_button and s.prize
                            and self._judge(show, s.prize) == "buyers"):
                        # An open buyers card has no Enter button. Retrying it ended in a 'missed' row after four
                        # reads, again after every flicker: the 25 'missed' rows on 9/24 were 6 buyers giveaways at
                        # tradersshoprips. Only the words or the listing flag decide: open pack cards with no button
                        # yet are retried ('Random Chinese Pack! #39' and 'FREE ONE PIECE ... #23' then got one).
                        self._skip_buyers(device, show, s, key, again)
                        return
                    log.info("card open=%s but no button: entries=%s prize=%r",
                             getattr(s, "prize", "") != "", s.entries, s.prize)
                    continue                            # still collapsed; try again
                p = getattr(self, "_pending", None)
                if again is not None and p and p["show"] == show.id and p["key"] == again.key:
                    # The tap our last row stands for never landed: this card still offers Enter. Drop that row and
                    # enter it the careful way, recorded 'redo', rather than leave it beside a new 'entered' row.
                    self._forget_the_tap(show, s)
                    again = None
                if self._foreign_only(show, getattr(s, "region_only", "")):
                    return
                verdict = self._judge(show, s.prize)
                if verdict == "buyers":
                    self._skip_buyers(device, show, s, key, again)
                    return
                if verdict == "leave":
                    # We came here because every giveaway listed was a pack or better. This one is not.
                    st.note(f"{s.prize or 'a giveaway with no readable prize'} is not a pack or better: "
                            f"leaving {show.seller} for the rest of this broadcast")
                    self._record_row(device, show, key, s.entries, "skipped", s.prize, again)
                    self._ended[show.id] = now + 12 * 3600
                    self._unpin(show, "not a pack or better")
                    self._left_why = "not_pack"
                    st.camped = None                    # step() picks the next stream on the next tick
                    return
                if verdict == "skip":
                    st.note(f"skipped {s.prize or '(unreadable prize)'}: junk (card, sticker, keychain...)")
                    self.turn.why = f"NOT entering {s.prize or 'this one'}: junk. Waiting for the next giveaway"
                    self._record_row(device, show, key, s.entries, "skipped", s.prize, again)
                    self._card_handled = True
                    return
                sc, parts = score_confirmed(_stub_event(show, now), s.prize, s.entries,
                                            self.cfg.scoring, self.followed)
                if sc < self.cfg.scoring.min_score_confirm:
                    st.note(f"skipped {s.prize or '?'} ({s.entries} entries, score {sc:.0f})")
                    self.notifier.plain(f"Skipped: {s.prize or '(unknown prize)'} — "
                                        f"{show.seller} ({s.entries} entries, score {sc:.0f})")
                    self._record_row(device, show, key, s.entries, "skipped", s.prize, again)
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
                    self._unverified(device, show, key, tapped, s, now, t_seen)
                    return
                if after.entered:
                    self._record_entered(device, show, key, s, sc, combined, t_seen)
                    return
            if tapped is not None:
                # tapped, never saw the confirmation, ran out of looks: count it, do not retry
                self._unverified(device, show, key, tapped, tapped[0], now, t_seen)
                return
            else:
                if (again is not None and again.result in ("entered", "unverified")
                        and (not s.prize or (again.prize and same_prize(again.prize, s.prize)))):
                    st.note("could not open the card in time; most likely the giveaway we already entered")
                else:
                    st.note("could not open the card in time")
                self._record_row(device, show, key, s.entries, "missed", s.prize, again)
            self._card_handled = True
        except Exception as e:
            self._error(device, "enter", e)

    def _skip_buyers(self, device, show, s, key: str, again) -> None:
        """The listing can be missing or wrong; the card's own words are not. Skipped, and we stay: a buyers
        giveaway beside open packs does not make the stream worse (agreed 2026-09-19)."""
        self.state.note(f"skipped {s.prize}: buyers-only giveaway")
        self.turn.why = f"NOT entering {s.prize}: buyers only. Waiting for the next giveaway"
        self._record_row(device, show, key, s.entries, "skipped", s.prize, again)
        self._card_handled = True

    def _record_row(self, device, show, key: str, entries, result: str, prize: str, again) -> None:
        """A 'skipped' or 'missed' row, unless this card is probably the giveaway our last row here is already about
        (`again`, with the same title or none readable): then that row stands for it and takes this Turn's peak."""
        if again is not None and (not prize or (again.prize and same_prize(again.prize, prize))):
            self.turn.key = again.key
            return
        self.store.record_entry(key, show, device.name, entries, result, account=self.account, prize=prize)
        self.turn.key = key

    LATE_CONFIRM_S = 900       # a tap we could not confirm is still ours if "You're in" shows up within this long

    def _listed_info(self, show):
        if self.listed_lookup is None:
            return None
        try:
            return (self.listed_lookup() or {}).get(show.id)
        except Exception:
            return None

    def _fast_ok(self, show, s, now: float) -> bool:
        """Fast enter taps Enter before the prize can be read, so it is only for streams where every listed
        giveaway is a pack or better that anyone may enter: no buyers giveaway, nothing else. Anywhere the
        prize COULD be something we skip, the card is read first as before."""
        from . import chooser
        if self.turn.no_fast:
            return False                               # a fast tap has already missed on this giveaway
        info = self._listed_info(show)
        if info is None or getattr(info, "kind", "") != chooser.PACK_ONLY or getattr(info, "buyers", 0):
            return False
        if s.entered or not s.card_visible:
            return False
        sc, _parts = score_confirmed(_stub_event(show, now), "", s.entries, self.cfg.scoring, self.followed)
        return sc >= self.cfg.scoring.min_score_confirm

    def _last_here(self, show, s, now: float):
        """The giveaway our last row here was about, when the card on screen may still be it; None = a new giveaway.

        The badge leaves the screen for 2-3 reads mid-giveaway (recorded 2026-09-23, reelsalty85 in tradersshoprips:
        10 times in 5 minutes). Two blank reads end the Turn, so when the badge came back fast enter tapped it and
        recorded it again: 48 of 281 rows 9/23 22:21 - 9/24 10:41, 11-252 s after the first, count never collapsed.
        Read from the database, so a restart does not forget it. An unreadable count counts as not collapsed: a wrong
        'maybe the same' costs one careful read (2-3 s), and speed no longer changes the odds."""
        p = self.cfg.pacing
        if not p.one_row_per_giveaway:
            return None
        row = self.store.last_row(self.account, show.id, now - p.same_giveaway_s)
        if row is None:
            return None
        top = max((x for x in (row.entries_seen, row.peak) if x is not None), default=None)
        return None if count_collapsed(top, s.entries) else row

    @staticmethod
    def _another_giveaway(again, s, enter_xy) -> bool:
        """The card, once read, is not the giveaway our last row here is about: another title, or (no title to go by)
        Enter on offer where that row says we are in. Not every 'maybe the same' is a flicker: of 199 next giveaways
        within 10 minutes on 9/23-24, 124 were first seen at a count that had not collapsed from the previous one's
        count at entry, and about 60 would not against a peak of 2.27x that (the median). Those are the ones first
        seen at higher counts: left out, the stream's crowd would look smaller than it is."""
        if s.prize and again.prize:
            return not same_prize(again.prize, s.prize)
        return enter_xy is not None and not s.entered and again.result == "entered"

    def _count_start(self, show, entries, now: float) -> None:
        """A new giveaway in this stream: for the stay (and its first read, for the trial report), for the ActivityBook
        (how often this stream REALLY runs them, and the count at entry its crowd model is calibrated on), and as the
        draw of the giveaway our entry here was in: one runs at a time (Visit.entry_drawn_at). Only a card counted as
        new sets the first read: a stay resumed after a restart used to take it from our own giveaway, at its later
        count (entered at 5, back 80 s later at 40: 'first read over 30')."""
        self.state.giveaways_seen_here += 1
        if self.visit.entry_at and self.cfg.pacing.one_row_per_giveaway:     # (off: a flicker counts as new)
            self.visit.entry_drawn_at = now
        if self.visit.first_entrants is None and entries is not None:
            self._first_read(entries)
        if self.activity is not None:
            self.activity.record(show.id, now)
            self.activity.note_entrants(show.id, entries)

    def _first_read(self, n: int) -> None:
        """The first entrant count read on a card in this stay, for the trial report: 9/19-9/24, stays whose first
        read was over 30 spent 28% of the stream time after it and won 0.10/h, the rest about 0.49/h. Saved as it is
        read, not when the stay closes: a pin from the window can replace self.visit first."""
        self.visit.first_entrants = n
        if self._visit_id is None:
            return                                   # the row opens at the end of this step and takes it then
        try:
            self.store.visit_first_entrants(self._visit_id, n)
        except Exception:
            log.exception("could not save the stay's first entrant count")

    def _follow_on_enter(self, show) -> bool:
        info = self._listed_info(show)
        return bool(getattr(info, "followers_only", 0))

    def _unverified(self, device, show, key: str, tapped, s, now: float, t_seen: float) -> None:
        """Tapped Enter, could not see the confirmation. Seen live: in break streams a Selecting Spot banner
        takes the card's place right after an auction, and nearly every one of these was a real entry. Counted
        against the hourly cap now, and recorded as an entry when the app next says we are in (_confirm_late)."""
        prize = tapped[0].prize or s.prize
        self.store.record_entry(key, show, device.name, tapped[0].entries, "unverified", account=self.account, prize=prize)
        self.turn.key = key
        self._pending = {"show": show.id, "key": key, "prize": prize, "entries": tapped[0].entries,
                         "sc": tapped[1], "combined": tapped[2], "t_seen": t_seen, "at": now}
        self._card_handled = True
        self._hop_note_entry(now)
        self.state.note(f"tapped Enter on {prize or '?'} but could not confirm; not retrying")

    TICK_EVERY_S = 5.0         # a screenshot each: only while a tap is waiting for proof, and not every poll

    def _ask_the_tick(self, device, show, ns, s, enter_xy, now: float) -> None:
        """A tap is waiting for proof and the badge is back, collapsed: its icon settles it without opening
        the card. The owner's pointer: a tick beside the entry count means we are in; a gift means we are not."""
        p = self._pending
        if (p["show"] != show.id or ns is None or not hasattr(device, "entered_by_tick") or s.entered
                or enter_xy is not None or not s.card_visible or now - getattr(self, "_tick_asked", 0.0) < self.TICK_EVERY_S):
            return
        self._tick_asked = now
        try:
            verdict = device.entered_by_tick(ns)
        except Exception:
            verdict = None
        if verdict is True:
            self._confirm_late(device, show, s, now, how="confirmed by the tick on the badge", by="tick")
        elif verdict is False:
            self._forget_the_tap(show, s)
            self.turn.reopen()                          # and enter it properly, from this read

    def _forget_the_tap(self, show, s) -> None:
        p = self._pending
        self._pending = None
        self.turn.no_fast = True                             # this giveaway gets the careful way: read, then tap
        self.store.drop_unverified(p["key"])             # it never happened: not against the hourly cap
        self.visit.hop_armed = False                     # nor is there an entry to hold the card back for: enter it
        # The redo enters this same giveaway: its socket id must not count as an earlier entry's (_hop_note_entry).
        self.visit.entry_socket_id = None
        self.state.note(f"the Enter tap on {s.prize or p['prize'] or 'that giveaway'} did not take; entering again")

    def _tap_did_not_take(self, show, s, enter_xy, now: float) -> bool:
        """We believe we tapped Enter and could not confirm it, and now the card is open with an Enter button
        on it and no "You're in". The app's own screen is the truth: that tap never landed. Seen live: a fast
        enter's predicted tap arrived before the card had finished opening, the confirmation look hit a
        banner, and the bot sat beside the open card for minutes. Only for a giveaway we MEANT to enter (a
        pending tap): one we skipped on purpose shows the same button."""
        p = getattr(self, "_pending", None)
        if not p or p["show"] != show.id or enter_xy is None or s.entered:
            return False
        self._forget_the_tap(show, s)
        return True

    def _confirm_late(self, device, show, s, now: float,
                      how: str = "confirmed late: the card was hidden when we first looked", by: str = "late") -> bool:
        """`by` goes on the row: 'late' ("You're in" on a later read) or 'tick' (the badge's icon)."""
        p = getattr(self, "_pending", None)
        if not p or p["show"] != show.id or now - p["at"] > self.LATE_CONFIRM_S:
            return False
        self._pending = None
        st = self.state
        if p["combined"]:
            self.followed.add(show.seller)
            self.store.record_follow(show.seller, self.account)
        self.store.confirm_entry(p["key"], s.prize, by=by)
        st.entered_here += 1
        self._card_handled = True
        self.turn.key = p["key"]
        self._hop_saw_entry(now)
        prize = s.prize or p["prize"]
        if not prize and self.prize_name_lookup is not None:
            try:
                prize = self.prize_name_lookup(show.id)
            except Exception:
                prize = ""
        prize = prize or "a pack giveaway"
        self._last_entered = {show.id: prize}
        st.note(f"ENTERED {prize} ({p['entries']} entries), {how}")
        self.notifier.plain(format_entry(prize, show.seller, show.game, p["entries"], p["sc"]))
        return True

    def _record_entered(self, device, show, key: str, s, sc: float, combined: bool, t_seen: float):
        st = self.state
        if combined:
            self.followed.add(show.seller)
            self.store.record_follow(show.seller, self.account)
        # 'redo': an earlier tap on this giveaway did not take, and this one was proven directly.
        self.store.record_entry(key, show, device.name, s.entries, "entered", account=self.account, prize=s.prize,
                                confirmed_by="redo" if self.turn.no_fast else "direct")
        self.turn.key = key
        st.entered_here += 1
        self._card_handled = True
        self._hop_note_entry(getattr(self, "_now", None) or time.time())
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

    MAX_PATIENCE_S = 900.0     # never sit longer than this waiting for one giveaway

    def _patience(self, show, now: float) -> float:
        """How long to sit with nothing happening, counted from `_idle_since` (arrival, or the card going).

        90 s for a stream with no known rhythm, so 'a pack every 5 minutes' is not abandoned while waiting
        for the next one. A stream in rhythm is waited for until its next giveaway is due plus half an
        interval of slack - measured from the stream's OWN last giveaway, not from our arrival. Seen live
        2026-09-20: a rhythm measured half an hour earlier bought a dormant 2-viewer stream a fresh quarter
        of an hour from every account that walked in, restart included: 14 minutes, no entries.
        """
        p = self.cfg.pacing
        base = float(p.switch_after_quiet_s if self.state.giveaways_seen_here else p.switch_if_never_active_s)
        if self.activity is None:
            return base
        gap = self.activity.interval(show.id, now)
        last = self.activity.last_start(show.id, now)
        if not gap or last is None:
            return base
        due_at = last + 1.5 * gap                    # late enough to give up on
        return max(base, min(self.MAX_PATIENCE_S, due_at - (self._idle_since or now)))

    def _pack_value(self, show, now: float, listed) -> float:
        """Expected PACK wins per hour in a stream if every giveaway there were entered: _hop_side's value, so the
        pack-only upgrade and the crowd hop judge every stream the same way."""
        return self._hop_side(show, now, listed).value

    def _upgrade(self, now: float) -> bool:
        """In a fallback stream (mixed / other): move when a free pack-only stream promises at least
        pacing.pack_upgrade_ratio x the pack wins per hour of this one (_upgrade_verdict). True = moving.

        It used to move as soon as ANY pack-only stream was free. The 26 such moves 9/19-9/24 were net neutral: the
        streams left made 0.60-0.61 pack wins/h, the ones moved to 0.59. E.g. dercz388 (4 viewers) -> hitvaultcards
        (99 viewers), 2026-09-20 11:32, and jhandedripz (2) -> rarevendy (34), 9/20 22:32. A stream with no pack
        listed (other) scores 0 here, so any pack-only stream still pulls the account out (John's 9/19 kind-first
        rule). Only while no card is on screen, and at most every UPGRADE_CHECK_S.

        Not while our entry here may still be open (pacing.upgrade_after_draw, _upgrade_waits): it used to go at the
        first read with no card, and the badge leaves the screen for 2-3 reads mid-giveaway, so it forfeited the entry
        (no win has ever been drawn to an account that left before the draw). 14 of the 27 upgrades 9/20-9/24 came
        within 240 s of an entry (entry to draw: median 275 s); 9/22 19:35:41 jgoblin22 left jushighbargains 69 s after
        entering 'Pack On Screen #5'. With the crowd hop live it is decided at that draw instead and leaves after the
        linger, like a hop (_plan_upgrade). A waiting look does not count as one of the looks every UPGRADE_CHECK_S.

        A move is voluntary: with the crowd hop on it writes an 'upgrade' decision row with the gate's own numbers, so
        the hourly cap (Store.voluntary_moves) and the trial report count it. In live mode it also waits at that cap,
        so hop.max_per_hour holds for every voluntary move; off or log, it is not capped (as before the crowd hop)."""
        st, visit = self.state, self.visit
        if st.camped is None or now - visit.upgrade_checked < self.UPGRADE_CHECK_S:
            return False
        if self.cfg.pacing.upgrade_after_draw and self._upgrade_waits(now):
            return False
        visit.upgrade_checked = now
        v = self._upgrade_verdict(now)
        if v is None:
            return False
        camped, best = st.camped, v.best
        if v.reason == hop.NOT_BETTER:
            if best.show.id != visit.upgrade_declined:             # once per candidate per stay, not every 30 s
                visit.upgrade_declined = best.show.id
                st.note(f"staying in {camped.seller}: the best free pack-only stream, {best.show.seller} "
                        f"({best.show.viewers} viewers), promises {best.value:.3f} pack wins/h against "
                        f"{v.cur.value:.3f} here (needs {self.cfg.pacing.pack_upgrade_ratio:g}x)")
            return False
        if v.reason == hop.CAP:
            if not visit.upgrade_capped:                           # once per stay, not every 30 s
                visit.upgrade_capped = True
                st.note(f"not moving to a pack-only stream yet: {v.moves} voluntary moves in the last hour "
                        f"(cap {self.cfg.hop.max_per_hour})")
            return False
        st.note(f"a pack-only stream is available ({best.show.seller}, {best.show.viewers} viewers, "
                f"{best.value:.3f} pack wins/h against {v.cur.value:.3f} here); leaving {camped.seller}")
        self._left_why = "upgrade"
        if self._hop_on():
            self._hop_log(now, hop.UPGRADE, hop.PACK_ONLY_FREE, v, hop.CHECK)
        st.camped = None
        return True

    def _upgrade_verdict(self, now: float) -> hop.Verdict | None:
        """The pack-only upgrade's gate, for _upgrade and _plan_upgrade. None: nothing to weigh (not in a stream, a
        stream you pinned, a pack-only or legacy stream, no free pack-only stream). Else leave for .best (reason
        PACK_ONLY_FREE), or stay: NOT_BETTER under pacing.pack_upgrade_ratio (both sides _pack_value: the same crowd
        model, current viewers), CAP at hop.max_per_hour (crowd hop live only)."""
        from . import chooser
        st = self.state
        camped = st.camped
        if camped is None or (st.pinned_url and camped.id in st.pinned_url):
            return None                                # you put it here
        if self._kind(camped) in (chooser.PACK_ONLY, chooser.LEGACY):
            return None
        extras = self._rank_extras(now)
        ranked = rank_streams(self._live_shows(), self.giveaway_counts, self.cfg, self.followed,
                              self.store.blacklisted_sellers(), self._others() | {camped.id},
                              set(self.store.favorites()), **extras)
        best = next((p for p in ranked if p.kind == chooser.PACK_ONLY and p.show.id not in self.cooldowns), None)
        if best is None:
            return None
        cur, dest = self._hop_side(camped, now, extras["listed"]), self._hop_side(best.show, now, extras["listed"])
        mode = self._hop_on()
        moves = self.store.voluntary_moves(self.account, now - 3600) if mode else 0
        ratio = self.cfg.pacing.pack_upgrade_ratio
        if ratio > 0 and dest.value < ratio * cur.value:
            return hop.Verdict(False, hop.NOT_BETTER, cur, dest, [], moves)
        if mode == hop.LIVE and moves >= self.cfg.hop.max_per_hour:
            return hop.Verdict(False, hop.CAP, cur, dest, [], moves)
        return hop.Verdict(True, hop.PACK_ONLY_FREE, cur, dest, [dest], moves)

    def _entry_open(self, now: float) -> bool:
        """Our entry here may still be open: made (or first seen) in this stay, no draw seen for it since
        (Visit.entry_drawn_at: its 'ended' from the socket, in every hop mode, or a new giveaway on screen), and not
        longer than hold_timeout_s ago."""
        v = self.visit
        return bool(v.entry_at) and not v.entry_drawn_at and now - v.entry_at <= self.cfg.pacing.hold_timeout_s

    def _upgrade_waits(self, now: float) -> bool:
        """pacing.upgrade_after_draw: not with a card dealt with still counted as on screen, not while our entry may be
        open, and with the crowd hop live not within its linger after that draw either (John: stay 20-30 s after it)."""
        v = self.visit
        if self.turn.handled or self._entry_open(now):
            return True
        return now < v.entry_drawn_at + self.cfg.hop.linger_s and self.cfg.hop.mode == hop.LIVE

    def _plan_upgrade(self, drawn_at: float, now: float, trigger: str, leave_at: float) -> bool:
        """Live mode, at a draw the crowd hop stays for: a pack-only upgrade due from this fallback stream is decided
        here and leaves after the linger, like a hop (a HopPlan with action UPGRADE, carried out by _hop_tick: nothing
        new entered meanwhile, the screen's proof, judged again at the moment of leaving). Without this, the next
        giveaway (a median 14 s after the draw) was entered first and the upgrade waited for that one's draw too. True =
        planned; its one row ('upgrade' or 'cancelled') comes when the plan ends."""
        if not self.cfg.pacing.upgrade_after_draw:
            return False
        v = self._upgrade_verdict(now)
        if v is None or not v.leave:
            return False
        self.visit.hop = hop.HopPlan(target_id=v.best.show.id, decided_at=now, drawn_at=drawn_at, trigger=trigger,
                                     verdict=v, action=hop.UPGRADE)
        self.state.note(f"leaving {v.cur.show.seller} after the draw for the pack-only stream {v.best.show.seller} "
                        f"({v.best.value:.3f} pack wins/h against {v.cur.value:.3f} here); going in "
                        f"{max(0, round(leave_at - now))}s")
        return True

    def _upgrade_due(self, now: float) -> bool:
        """Would the pack-only upgrade leave if our draw came now? Live mode arms on it as on a crowd hop (_hop_tick),
        so the next giveaway (27% open in the same socket frame as the draw) is not entered before the draw is judged."""
        if not self.cfg.pacing.upgrade_after_draw:
            return False
        v = self._upgrade_verdict(now)
        return v is not None and v.leave

    def _maybe_move_on(self, now: float):
        """Called while no giveaway is on screen."""
        st = self.state
        if self.visit.hop is not None:
            return                                     # a crowd hop is pending: it decides, after its linger
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
        self._left_why = "quiet"
        st.camped = None                               # step() will move next tick

    def _leave_if_dead(self, now: float):
        """'Stay in one stream' = stay until the stream ends or runs no giveaway for pacing.single_quiet_s
        (10 min), then back to Scan streams (John, 2026-09-25). The stream ending is handled where it is
        noticed; both go through _unpin, which flips the mode.

        It never hops to something that merely ranks higher; that is what Scan mode is for. It used to pick a
        replacement and stay in 'single' mode: seen live 2026-09-19, pinned streams ended overnight and the bot
        sat in silent 3-viewer replacements for up to 3.7 h. Back in Scan, the scanner's own patience applies.
        """
        st = self.state
        camped = st.camped
        if camped is None:
            return
        p = self.cfg.pacing
        quiet_for = now - (self._idle_since or now)
        if quiet_for < p.single_quiet_s:
            return
        self.cooldowns[camped.id] = now + p.quiet_cooldown_s
        self._unpin(camped, f"no giveaway for {quiet_for / 60:.0f} min")
        ranked = rank_streams(self._live_shows(), self.giveaway_counts, self.cfg, self.followed,
                              self.store.blacklisted_sellers(), self._others() | {camped.id},
                              set(self.store.favorites()), **self._rank_extras(now))
        if not ranked:
            self._idle_since = now              # nowhere else to go: Scan looks again after its own quiet spell
            return
        st.note(f"no giveaway in {camped.seller} for {quiet_for / 60:.0f} min; moving to {ranked[0].show.seller}")
        self._left_why = "quiet"
        st.camped = None                        # step() picks the next one on the next tick

    # ---- crowd hop (item E) ----
    # The rule is givvy/hop.py. This is the glue: what the socket says about the stream we are in (socket_events, from
    # Engine._giveaway_seen), the entry facts of the stay (Visit), both sides valued on the one model, and one
    # hop_decisions row per judged draw. [hop] mode is read at every decision, so Settings changes it at once. 'log'
    # only writes. 'live' also arms while our entry is open and holds new cards back while armed or leaving
    # (_hop_tick, _hop_holds_entry), and leaves after the linger once the screen has proved the draw (_hop_tick).
    DRAW_MIN_AFTER_ENTRY_S = 45    # entry to draw: median 275 s, p10 124 s (tracker match 9/17-9/24). An 'ended' this
                                   # soon after our entry is the previous giveaway's frame arriving late
    STALE_SOCKET_S = 30            # an 'ended' taken off the queue later than this is too old to act on
    START_LATE_S = 30              # the socket reports a start 0-20 s late: an 'active' this soon after our entry is
                                   # the giveaway we entered
    HOP_ARM_EVERY_S = 30           # live, while an entry is open: how often 'would I leave if the draw happened now?'
                                   # is asked again (one rank_streams each)

    def _hop_note_entry(self, now: float) -> None:
        """We entered here: recorded, or tapped and not confirmed. The socket's id for it when the socket has already
        reported the start; otherwise _hop_drain fills it in when the 'active' comes. Live mode arms for this entry
        afresh at the next step (_hop_tick).

        One entry per giveaway (pacing.one_row_per_giveaway), so the id the last entry here was in is over for us. The
        phone usually sees a start first (133 of 148 camped starts before the socket, 9/23-24), and 27% of draws come
        in one frame with the next start: an entry while the socket still names the last one's giveaway is in the next
        one, whose id the socket has not sent yet. Keyed to the old id, the old 'ended' was taken for our draw when it
        came late, and our real draw never was."""
        v = self.visit
        if v.entry_socket_id is not None and self.cfg.pacing.one_row_per_giveaway:
            v.old_socket_ids.add(v.entry_socket_id)
        live = v.socket_live[0] if v.socket_live else None
        v.entry_at = v.entry_seen_at = now
        v.entry_socket_id = live if live not in v.old_socket_ids else None
        v.entry_drawn_at = 0.0
        v.hop_armed, v.hop_checked = False, 0.0

    def _hop_saw_entry(self, now: float) -> None:
        """A read showed our entry ('You're in', the tick on the badge). One first seen this way (after a restart)
        dates from now."""
        v = self.visit
        v.entry_seen_at = now
        if not v.entry_at:
            v.entry_at = now

    def _hop_on(self) -> str:
        """'live' or 'log' when draws here are judged, else ''. Not judged: the switch off (anything but live or log),
        nothing to judge with (no watcher or ActivityBook: tests and older callers), not in a stream, 'stay in one
        stream' mode, a stream you pinned, or a stream with no giveaway list (legacy, not read yet): there is no pack
        value to compare, so it adds no movement."""
        from . import chooser
        st, mode = self.state, self.cfg.hop.mode
        camped = st.camped
        if (mode not in hop.ON or self.watcher is None or self.activity is None or camped is None
                or st.mode != "scan" or (st.pinned_url and camped.id in st.pinned_url)):
            return ""
        return "" if self._kind(camped) in (chooser.LEGACY, chooser.UNKNOWN) else mode

    def _hop_side(self, show, now: float, listed, age: float | None = None) -> hop.Side:
        """A stream valued for the crowd hop and the pack-only upgrade: chooser.value_of (the one crowd model,
        givvy/crowd.py) x chooser.pack_share = expected pack wins per hour if every giveaway there were entered. The
        stream we are in and every candidate are valued exactly this way: on the old 2 + 0.18 x viewers the stream we
        were in looked 2-3x worse than every candidate. From the latest discovery entry: the camped LiveShow keeps the
        viewers it had when we joined (_move_to)."""
        from . import chooser
        fresh = next((s for s in self.shows if s.id == show.id), show)
        info = (listed or {}).get(show.id)
        kind = getattr(info, "kind", "") or (chooser.LEGACY if not listed else chooser.UNKNOWN)
        v = chooser.value_of(fresh, self.activity, now, self.giveaway_counts.get(show.id, 0))
        share = chooser.pack_share(info, kind)
        seen = self.activity.observed(show.id) if self.activity is not None else []
        return hop.Side(fresh, kind, fresh.viewers, v.entrants, v.per_hour, share, v.wins_per_hour * share,
                        statistics.median(seen) if seen else None, age)

    def _hop_sides(self, now: float, need_fresh: bool, leave_at: float) -> tuple:
        """(the stream we are in, the candidates in rank order). A candidate is free (streams our other accounts hold
        never appear), not cooling after a quiet leave, the same kind or better (pack-only first: a pack-only stream
        only moves to a pack-only one), and has a proven rhythm (two starts in the ActivityBook). `need_fresh`: it also
        has a giveaway the socket SAW start at most hop.fresh_s before `leave_at` (age_s)."""
        from . import chooser
        camped = self.state.camped
        extras = self._rank_extras(now)
        listed = extras["listed"]
        cur = self._hop_side(camped, now, listed)
        floor = chooser.KIND_RANK.get(cur.kind, 0)
        fresh: dict = {}
        if need_fresh:
            try:
                fresh = self.watcher.fresh_giveaways()
            except Exception:
                log.exception("could not ask the socket for fresh giveaways")
        ranked = rank_streams(self._live_shows(), self.giveaway_counts, self.cfg, self.followed,
                              self.store.blacklisted_sellers(), self._others() | {camped.id},
                              set(self.store.favorites()), **extras)
        cands = []
        for p in ranked:
            sid = p.show.id
            if p.excluded or sid in self.cooldowns or chooser.KIND_RANK.get(p.kind, 0) < floor:
                continue
            age = None
            if need_fresh:
                if sid not in fresh or leave_at - fresh[sid] > self.cfg.hop.fresh_s:
                    continue
                age = leave_at - fresh[sid]
            if self.activity.interval(sid, now) is None:
                continue
            cands.append(self._hop_side(p.show, now, listed, age))
        return cur, cands

    def _hop_verdict(self, now: float, need_fresh: bool, leave_at: float | None = None) -> hop.Verdict:
        cur, cands = self._hop_sides(now, need_fresh, now if leave_at is None else leave_at)
        return hop.judge(cur, cands, self.cfg.hop, self.store.voluntary_moves(self.account, now - 3600))

    def _hop_log(self, now: float, action: str, reason: str, verdict, trigger: str) -> None:
        """One hop_decisions row: the stream we are in and the best candidate on one scale, for the trial report and
        the hourly cap."""
        try:
            self.store.record_hop(now, self.account, self.cfg.hop.mode, action, reason, trigger,
                                  cur=verdict.cur.row() if verdict else None,
                                  best=verdict.best.row() if verdict and verdict.best else None,
                                  moves=verdict.moves if verdict else 0)
        except Exception:
            log.exception("could not record the crowd-hop decision")

    def _hop_drain(self, show, now: float) -> None:
        """What the socket said about the stream we are in since the last step, oldest first. 'active': the giveaway
        running here, and the id of the one our entry is in when that start is reported after we entered. 'ended': a
        draw, judged (_hop_on_draw) only when it is fresh, no move is pending already, and it is OUR draw: the giveaway
        our entry is in, or any draw when this stay has no entry. Our draw is noted in every hop mode
        (Visit.entry_drawn_at): the pack-only upgrade waits for it.

        An 'ended' within DRAW_MIN_AFTER_ENTRY_S of our entry is the giveaway before ours, reported late: never ours
        later either (Visit.old_socket_ids), and if our entry had been keyed to it (the socket had not caught up when we
        entered), the entry is keyed afresh by the next 'active'."""
        v = self.visit
        while self.socket_events:                   # only this thread takes; the socket thread only appends
            status, sid, pid, t, started, saw = self.socket_events.popleft()
            if sid != show.id:
                continue
            if status == "active":
                v.socket_live = (pid, started, saw)
                if (v.entry_at and v.entry_socket_id is None and pid not in v.old_socket_ids
                        and t - v.entry_at <= self.START_LATE_S):
                    v.entry_socket_id = pid
                continue
            if v.socket_live is not None and v.socket_live[0] == pid:
                v.socket_live = None
            if pid in v.old_socket_ids:
                continue
            if v.entry_at and t - v.entry_at < self.DRAW_MIN_AFTER_ENTRY_S:
                v.old_socket_ids.add(pid)
                if v.entry_socket_id == pid:
                    v.entry_socket_id = None
                continue
            ours = bool(v.entry_at) and v.entry_socket_id in (None, pid)
            if ours:
                v.entry_drawn_at = t
            if now - t > self.STALE_SOCKET_S:
                continue
            if v.hop is None and (not v.entry_at or ours):
                self._hop_on_draw(t, now, hop.SOCKET)

    def _hop_on_draw(self, drawn_at: float, now: float, trigger: str) -> None:
        """A draw in the stream we are in (the socket's 'ended', or trigger 'screen': a new giveaway seen while armed):
        stay, or leave for a clearly better stream (givvy/hop.py), judged for a leave hop.linger_s after the later of
        the draw and this decision (_hop_tick waits that long), when the candidate's giveaway must still be fresh. One
        row per judged draw. In live mode a leave is only planned here (Visit.hop): its row is written when the plan
        ends, left or cancelled (_hop_tick). A stay in live mode can still plan the pack-only upgrade (_plan_upgrade)."""
        visit = self.visit
        visit.hop_armed = False
        mode = self._hop_on()
        if not mode:
            return
        visit.hop_judged_entry = visit.entry_at
        leave_at = max(drawn_at, now) + self.cfg.hop.linger_s
        try:
            v = self._hop_verdict(now, need_fresh=True, leave_at=leave_at)
        except Exception:
            log.exception("[%s] crowd hop: could not judge the draw", self.account)
            return
        cur, best = v.cur, v.best
        if not v.leave:
            try:
                planned = mode == hop.LIVE and self._plan_upgrade(drawn_at, now, trigger, leave_at)
            except Exception:
                log.exception("[%s] crowd hop: could not weigh the pack-only upgrade", self.account)
                planned = False
            if planned:
                return                                 # its row comes when that plan ends
            self._hop_log(now, hop.STAYED, v.reason, v, trigger)
            # About 11-12 draws an account-hour: the log only, not the window.
            log.info("[%s] crowd hop: staying in %s after the draw (%s), ~%.0f at the draw%s", self.account,
                     cur.show.seller, v.reason, cur.crowd,
                     f"; best free {best.show.seller} ~{best.crowd:.0f}" if best else "")
            return
        if mode == hop.LOG:
            self._hop_log(now, hop.WOULD_LEAVE, hop.BETTER, v, trigger)
            self.state.note(f"would leave {cur.show.seller} after the draw for {best.show.seller}: ~{cur.crowd:.0f} vs "
                            f"~{best.crowd:.0f} at the draw (crowd hop: {mode})")
            return
        visit.hop = hop.HopPlan(target_id=best.show.id, decided_at=now, drawn_at=drawn_at, trigger=trigger, verdict=v)
        better = f"{best.value / cur.value:.1f}x better" if cur.value > 0 else "better (nothing here scores)"
        self.state.note(f"leaving {cur.show.seller} after the draw: {best.show.seller} looks {better}; going in "
                        f"{max(0, round(leave_at - now))}s")

    def _hop_cancel(self, now: float, reason: str, verdict=None) -> bool:
        """The pending move is off (a hop reason): the draw's one row, 'cancelled', with `verdict` (judged just now) or
        the one taken at the draw, and the stay goes on. A new giveaway on screen is entered by the normal flow in this
        same step, 25-30 s late, which costs nothing: giveaways run a median 204-251 s, and speed no longer changes the
        odds. Our entry seen after the draw signal (OPEN) means that signal was not our draw: the pack-only upgrade
        goes back to waiting for it. False, so _hop_tick can end with it."""
        visit = self.visit
        p, visit.hop = visit.hop, None
        v = verdict or p.verdict
        self._hop_log(now, hop.CANCELLED, reason, v, p.trigger)
        self.state.note(f"staying in {v.cur.show.seller}: {hop.WORDS.get(reason, reason)}")
        visit.hop_armed, visit.hop_judged_entry = False, visit.entry_at
        if reason == hop.OPEN:
            visit.entry_drawn_at = 0.0
        return False

    def _hop_drop(self, now: float) -> None:
        """The stay ends some other way while a move is pending (the stream ended, a pin, the Blacklist button, the
        device went away, Stop): that judged draw still gets its one row."""
        p = self.visit.hop
        if p is not None:
            self.visit.hop = None
            self._hop_log(now, hop.CANCELLED, hop.STAY_ENDED, p.verdict, p.trigger)

    def _hop_tick(self, device, show, s, ns, enter_xy, now: float) -> bool:
        """Live mode, at every read in the stream (after the won check). True = it has just moved the account.

        No move pending: while our entry is open (hold_timeout_s at most), 'would I leave if the draw happened now?'
        every HOP_ARM_EVERY_S, candidates needing no fresh giveaway yet, and the same for a pack-only upgrade due from a
        fallback stream (_plan_upgrade). Armed, the account enters nothing new here until that draw is judged
        (_hop_holds_entry). An arm whose draw is never seen, by the socket or on screen, expires at entry_at +
        hold_timeout_s with a 'stayed' draw_not_seen row.

        A move pending (Visit.hop, a crowd hop or a pack-only upgrade): called off by the switch, a win here, or any
        sign our entry is still open ('You're in', the tick on the badge, a newer entry). It waits linger_s after the
        later of the draw and the decision: a draw signal taken off the queue late (up to STALE_SOCKET_S, behind a live
        check of up to 20 s) used to leave after two blank reads, which is the normal mid-giveaway flicker (1481 blank
        spells on 9/24, p50 3 s). Then it needs the screen's proof that our giveaway is gone: no badge in at least two
        reads, or a badge proven to be one we are not in (an Enter button, the gift icon). About 5% of the socket's id
        changes came mid-giveaway, so its 'ended' alone is never enough. Then the rule is judged again with the fresh
        giveaway measured from now, and the first candidate in rank order that can still be claimed is the one."""
        visit, cfg = self.visit, self.cfg.hop
        p = visit.hop
        if p is None:
            if cfg.mode != hop.LIVE or not visit.entry_at or visit.entry_at == visit.hop_judged_entry:
                return False
            if now - visit.entry_at <= self.cfg.pacing.hold_timeout_s:
                if now - visit.hop_checked >= self.HOP_ARM_EVERY_S:
                    visit.hop_checked = now
                    visit.hop_armed = self._hop_on() == hop.LIVE and (self._hop_verdict(now, need_fresh=False).leave
                                                                      or self._upgrade_due(now))
            elif visit.hop_armed:
                visit.hop_armed, visit.hop_judged_entry = False, visit.entry_at
                self.state.note(f"the draw of our entry in {show.seller} was never seen; not holding new giveaways "
                                f"back any more")
                self._hop_log(now, hop.STAYED, hop.UNSEEN, self._hop_verdict(now, need_fresh=False), hop.TIMEOUT)
            return False
        if getattr(s, "in_stream", True):
            p.reads += 1
        if cfg.mode != hop.LIVE or self._hop_on() != hop.LIVE:
            return self._hop_cancel(now, hop.OFF)
        if s.won:
            return self._hop_cancel(now, hop.WON)
        if s.entered or visit.entry_seen_at > p.decided_at or visit.entry_at > p.decided_at:
            return self._hop_cancel(now, hop.OPEN)
        if s.card_visible:
            p.badge_seen = True
            if enter_xy is not None and not s.entered:
                p.not_ours = True                      # an Enter button: a giveaway we are not in
            elif ns is not None and hasattr(device, "entered_by_tick") and now - p.tick_at >= self.TICK_EVERY_S:
                p.tick_at = now
                try:
                    ours = device.entered_by_tick(ns)
                except Exception:
                    ours = None
                if ours is True:
                    visit.entry_seen_at = now
                    return self._hop_cancel(now, hop.OPEN)
                if ours is False:
                    p.not_ours = True                  # the gift icon
        if now < max(p.drawn_at, p.decided_at) + cfg.linger_s:
            return False
        if p.badge_seen and not p.not_ours:
            return self._hop_cancel(now, hop.UNPROVEN)
        if not p.badge_seen and p.reads < 2:
            return False                               # one blank read is no proof: the badge flickers
        upgrade = p.action == hop.UPGRADE
        v = self._upgrade_verdict(now) if upgrade else self._hop_verdict(now, need_fresh=True, leave_at=now)
        if v is None or not v.leave:
            return self._hop_cancel(now, hop.NO_CANDIDATE if v is None else v.reason, v)
        side = next((c for c in v.ok if self._claim(c.show)), None)   # our claim moves in one step: no gap
        if side is None:
            return self._hop_cancel(now, hop.CLAIM, v)
        visit.hop = None
        self._hop_log(now, p.action, v.reason, replace(v, best=side), p.trigger)
        if upgrade:
            self.state.note(f"left {show.seller} after the draw for the pack-only stream {side.show.seller}: "
                            f"{side.value:.3f} pack wins/h against {v.cur.value:.3f} here, {v.moves + 1} of "
                            f"{cfg.max_per_hour} moves this hour")
        else:
            self.state.note(f"left {show.seller} after the draw for {side.show.seller}: ~{v.cur.crowd:.0f} vs "
                            f"~{side.crowd:.0f} entrants expected, {v.moves + 1} of {cfg.max_per_hour} moves this hour")
        self._left_why = "upgrade" if upgrade else "hop"
        self.state.camped = None
        self._move_to(device, side.show, now)          # a failed open: released, _ended, _open_failed, pick again
        return True

    def _hop_holds_entry(self, device, show, s, ns, enter_xy, now: float) -> bool:
        """Live mode, where a new entry would start (a card up that is not dealt with): hold it back? True = not now;
        turn.why and the window say why.

        Leaving after a draw: nothing new is entered. Armed: the card may be our own giveaway back after a flicker, or
        the next one after a draw the socket missed. Within DRAW_MIN_AFTER_ENTRY_S of our entry it is ours. An Enter
        button is a giveaway we are not in, so our draw has happened: judged now (trigger 'screen'), and a stay enters
        the card at once. With no button the badge's icon tells, one screenshot per TICK_EVERY_S: the tick is ours (a
        flicker: the Turn goes on the row we entered it under, nothing is tapped), the gift is a new giveaway (our
        draw), neither is 'look again', bounded by the arm's expiry at entry_at + hold_timeout_s (worst case the next
        giveaway is entered about 145 s late, still inside its ~4-minute timer). A device that cannot read the icon is
        never held: E then relies on the socket's draw alone, and still never leaves with an entry open.

        Never held while our entry here is a tap nobody proved (Camper._pending): the card goes to _enter_here, which
        confirms it late ("You're in") or redoes it (an Enter button). Held, the gift on a giveaway whose own Enter tap
        never landed was read as 'our draw has happened': the account left and the giveaway was never entered."""
        visit = self.visit
        if self.cfg.hop.mode != hop.LIVE:
            return False
        if visit.hop is not None:
            return self._hop_hold("not entering: leaving this stream after the draw")
        p = getattr(self, "_pending", None)
        if p and p["show"] == show.id and p["at"] >= visit.entry_at:
            return False
        if not visit.hop_armed:
            return False
        if self._hop_on() != hop.LIVE:                 # pinned, or 'stay in one stream', since it was armed
            visit.hop_armed = False
            return False
        if now - visit.entry_at < self.DRAW_MIN_AFTER_ENTRY_S:
            return self._hop_hold("still in the giveaway we entered")
        if enter_xy is not None and not s.entered:
            ours = False
        elif ns is None or not hasattr(device, "entered_by_tick"):
            return False
        elif now - visit.hop_tick_at < self.TICK_EVERY_S:
            return self._hop_hold("checking whether this is the giveaway we entered")
        else:
            visit.hop_tick_at = now
            try:
                ours = device.entered_by_tick(ns)
            except Exception:
                ours = None
        if ours is None:
            return self._hop_hold("checking whether this is the giveaway we entered")
        if ours:                                       # the tick: our giveaway, back after a flicker
            self._hop_saw_entry(now)
            self._card_handled = True
            if not self.turn.key:                      # its peak goes on the row we entered it under
                row = self.store.last_row(self.account, show.id, now - self.cfg.pacing.same_giveaway_s)
                if row is not None and row.result in ("entered", "unverified"):
                    self.turn.key = row.key
            return self._hop_hold("still in the giveaway we entered")
        visit.entry_drawn_at = now                     # a giveaway we are not in: our draw has happened
        self._hop_on_draw(now, now, hop.SCREEN)
        return visit.hop is not None and self._hop_hold("not entering: leaving this stream after the draw")

    def _hop_hold(self, why: str) -> bool:
        self.turn.why = self.state.last_action = why
        return True

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
