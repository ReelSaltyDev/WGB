"""Desktop window for the givvy bot.

Tkinter, so no extra dependency. A normal window: opens maximised the first time,
then comes back the way you left it, minimises to the taskbar, and its activity
log collapses so it can sit narrow beside the emulators.

Controls:
  Devices  one row per phone/emulator; devices sharing an account are alternatives
  Mode     stay in one stream, or scan and move when the current one goes quiet
  Stream   double-click to sit in it with the chosen account
  Run      start / pause / stop, plus a merged activity log

`python -m givvy.app --start` presses Start on launch.
"""
from __future__ import annotations

import asyncio
import collections
import json
import queue
import sys
import threading
import time
import logging
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .accounts import AccountRunner, Claims, TaggedNotifier, build_device, group_by_account, rename_in_config
from .camper import Camper, rank_streams
from .config import load
from .device.adb import list_devices
from .discovery import Discovery
from .giveaway_info import GiveawayDirectory
from .notify import Notifier
from .store import Store
from .watcher import Watcher
from .whatnot_api import WhatnotClient
from .winshell import brand_window, set_process_app_id

REFRESH_MS = 700
FLASH_MS = 5000
LOGO = Path(__file__).resolve().parent / "assets" / "timebomb-logo.png"
# The stream list is grouped by viewer count (a user's idea): 600 live streams in one
# flat list is unreadable. Lower bounds, biggest first.
BANDS = ((1000, "1000+ viewers"), (500, "500-999"), (300, "300-499"), (200, "200-299"), (100, "100-199"),
         (70, "70-99"), (50, "50-69"), (30, "30-49"), (20, "20-29"), (10, "10-19"), (0, "under 10"))
OPEN_BY_DEFAULT = 3                                # bands expanded on a fresh install: the three biggest


def band_of(viewers: int) -> str:
    return next(label for lo, label in BANDS if viewers >= lo)
ICON = Path(__file__).resolve().parent / "assets" / "givvy.ico"
log = logging.getLogger(__name__)


MODES = {"scan": "Scan streams", "single": "Stay in one stream"}
KIND_LABEL = {"pack_only": "PACKS ONLY", "mixed": "packs + other", "other": "no pack", "legacy": ""}


def save_modes(ui: dict, engine) -> None:
    ui["modes"] = engine.modes()


def load_modes(ui: dict, engine) -> None:
    """Put each account back in the mode it was left in. Unknown accounts and values are ignored."""
    for account, mode in (ui.get("modes") or {}).items():
        engine.set_mode(account, mode)


class Engine:
    """Shared discovery + socket watcher on one thread, and one camper thread per
    account. A screen read blocks for ~2.6s, so accounts cannot share a loop."""

    def __init__(self, cfg, on_event):
        self.cfg = cfg
        self.on_event = on_event
        self.client = WhatnotClient()
        self.store = Store(cfg.root / cfg.paths.db)
        self.notifier = Notifier(cfg.discord, skip_history=True)
        self.discovery = Discovery(self.client, cfg.whatnot.feeds,
                                   cfg.whatnot.followed_sellers + self.store.followed_sellers(),
                                   cfg.whatnot.feed_page_size, cfg.whatnot.feed_max_pages,
                                   home_country=cfg.whatnot.home_country if cfg.whatnot.only_home_country else "")
        self.watcher = Watcher(self.client, cfg.whatnot.client_version,
                               cfg.whatnot.watch_slots, on_event=self._giveaway_seen)
        for seller in self.store.favorites():
            self.discovery.add_seller(seller)
        self.claims = Claims()
        from .giveaway_info import set_words
        set_words(cfg.scoring.wanted_words, cfg.scoring.unwanted_words)
        self.giveaways = GiveawayDirectory(self.client)      # what each stream is giving away
        from .chooser import ActivityBook
        # How often each stream REALLY runs giveaways. Saved as it is learned and read back on start: it used
        # to be forgotten on every restart, and the scanner had to relearn every stream's rhythm from scratch.
        self.activity_book = ActivityBook(sink=self.store.record_activity)
        try:
            self.store.prune_activity(older_than=time.time() - 24 * 3600)
            self.activity_book.load(self.store.load_activity(since=time.time() - ActivityBook.WINDOW_S))
        except Exception:
            log.exception("could not read back what was learned about streams")
        self.cooldowns: dict[str, float] = {}                # streams just left for being dead; shared by all accounts
        self.giveaway_counts: dict[str, int] = {}      # shared: where giveaways actually happen
        self.activity: collections.deque = collections.deque(maxlen=300)
        self.connected: dict[str, str] = {}            # adb serial -> state, refreshed off the UI thread
        self.runners: list[AccountRunner] = []
        self.shows = []
        self._stop = threading.Event()
        self._refresh_asked = threading.Event()
        self.last_discovery = 0.0
        for label, accs in group_by_account(cfg.accounts).items():
            self._make_runner(label, accs)
        self._retag()
        try:                                              # Win Charts: the wins from before it existed, once
            from .wincharts import backfill
            only = self.runners[0].account if len(self.runners) == 1 else ""
            n = backfill(self.store, cfg.root / cfg.paths.log, single_account=only)
            if n:
                log.info("win history: %d earlier wins read back from the log", n)
        except Exception:
            log.exception("could not read earlier wins from the log")
        if cfg.paths.givvy_wins_db:
            threading.Thread(target=self._follow_tracker, name="givvy-wins", daemon=True).start()
        from .screens import keep_tidy                    # the screenshots folder used to grow ~35 MB a day, forever
        threading.Thread(target=keep_tidy, name="screens-cleanup", daemon=True,
                         args=(Path(cfg.paths.screens_dir), cfg.paths.screens_keep_days,
                               cfg.paths.screens_max_mb)).start()
        self._thread: threading.Thread | None = None
        self.crashes = 0
        self.last_tick = 0.0

    def _follow_tracker(self):
        """Win Charts from the Givvy Wins tracker's database, every 5 minutes (read-only)."""
        from .wincharts import TRACKER_EVERY_S, import_tracker
        path = Path(self.cfg.paths.givvy_wins_db)
        if not path.is_absolute():
            path = self.cfg.root / path
        said = False
        while True:
            try:
                if path.exists():
                    n = import_tracker(self.store, path, [r.account for r in self.runners])
                    if n["added"] or n["matched"] or not said:
                        log.info("win history from the Givvy Wins tracker: %d new, %d the bot had seen, %d known",
                                 n["added"], n["matched"], n["known"])
                elif not said:
                    log.warning("Givvy Wins tracker database not found: %s", path)
            except Exception:
                log.exception("could not read the Givvy Wins tracker")
            said = True
            time.sleep(TRACKER_EVERY_S)

    def _make_runner(self, label: str, accs) -> AccountRunner:
        runner = AccountRunner(label, [build_device(a, self.cfg) for a in accs], camper=None)
        for a in accs:
            runner.enabled[a.name] = a.enabled
        camper = Camper(self.cfg, self.store, self.notifier, runner.current_device, watcher=self.watcher,
                        followed=set(self.cfg.whatnot.followed_sellers), account=label, claims=self.claims)
        camper.giveaway_counts = self.giveaway_counts
        camper.state.sink = self.activity.append
        camper.on_prize_seen = self.giveaways.note_seen
        camper.listed_lookup = self.giveaways.summary
        camper.cooldowns = self.cooldowns
        camper.live_check = self._still_live
        camper.buyers_title_lookup = self.giveaways.is_buyers_title
        camper.prize_grade_lookup = self.giveaways.grade_for
        camper.prize_desc_lookup = self.giveaways.description_for
        camper.prize_name_lookup = self.giveaways.likely_title
        camper.activity = self.activity_book
        camper.set_shows(self.shows)
        runner.camper = camper
        self.runners.append(runner)
        return runner

    def _retag(self):
        """With more than one account, every log and Discord line says whose it is."""
        multi = len(self.runners) > 1
        for r in self.runners:
            r.camper.state.tag = r.account if multi else ""
            r.camper.notifier = TaggedNotifier(self.notifier, r.account) if multi else self.notifier

    def add_account(self, a) -> AccountRunner:
        """A new emulator joins the running bot. It used to need a restart, and the
        only hint was one line in the activity log."""
        existing = self.runner(a.account or a.name)
        if existing is not None:
            return existing
        self.cfg.accounts.append(a)
        runner = self._make_runner(a.account or a.name, [a])
        self._retag()
        if self.running:
            runner.thread = threading.Thread(target=self._camp_loop, args=(runner,), daemon=True)
            runner.thread.start()
        return runner

    def set_mode(self, account: str, mode: str) -> None:
        """Scan / stay-in-one-stream is per account: each emulator does its own thing."""
        runner = self.runner(account)
        if runner is None or mode not in MODES:
            return
        if runner.camper.state.mode != mode:
            runner.camper.set_mode(mode)

    def modes(self) -> dict[str, str]:
        return {r.account: r.camper.state.mode for r in self.runners}

    def rename(self, old_name: str, new_name: str, new_account: str):
        """Rename a device and/or its account: config.toml, history, and the
        running bot, in place. Raises ValueError with a readable reason."""
        old_acc, new_acc = rename_in_config(self.cfg.root / "config.toml", old_name, new_name, new_account)
        runner = self.runner(old_acc)
        if new_acc != old_acc:
            self.store.rename_account(old_acc, new_acc)
            self.claims.release(old_acc)             # the camper re-claims under its new name next tick
            runner.account = runner.camper.account = new_acc
        for d in runner.devices:
            if d.name == old_name:
                d.name = new_name
        runner.enabled = {(new_name if k == old_name else k): v for k, v in runner.enabled.items()}
        for a in self.cfg.accounts:
            if a.account == old_acc:
                a.account = new_acc
            if a.name == old_name:
                a.name = new_name
        self._retag()
        self.note(f"renamed {old_name} -> {new_name}" + (f" (account {old_acc} -> {new_acc})" if new_acc != old_acc else ""))

    def note(self, msg: str):
        self.activity.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        log.info(msg)

    def add_favorite(self, seller: str):
        seller = seller.strip().lstrip("@").lower()
        if seller:
            self.store.favorite_add(seller)
            self.discovery.add_seller(seller)      # found from the next poll even if it is a small stream
            self.note(f"favorite added: {seller}")

    def remove_favorite(self, seller: str):
        if self.store.favorite_remove(seller):
            self.note(f"favorite removed: {seller}")

    def runner(self, account: str) -> AccountRunner | None:
        return next((r for r in self.runners if r.account == account), None)

    def followed_any(self) -> set[str]:
        out: set[str] = set()
        for r in self.runners:
            out |= r.camper.followed
        return out

    def _giveaway_seen(self, ev):
        if ev.status == "active":
            self.giveaway_counts[ev.show.id] = self.giveaway_counts.get(ev.show.id, 0) + 1
            if getattr(ev, "saw_start", True):         # one already running as we tuned in says nothing about WHEN
                self.activity_book.record(ev.show.id, time.time())
            for r in self.runners:                     # the account sitting in that stream looks NOW, not at its
                camped = r.camper.state.camped         # next scheduled poll (up to 3 s away)
                if camped is not None and camped.id == ev.show.id:
                    r.wake.set()

    def _discover(self) -> bool:
        """Re-check which streams are live. Keeps the old list if Whatnot cannot be reached."""
        try:
            shows = self.discovery.poll()
        except Exception as e:
            self.note(f"discovery failed: {e}")
            return False
        self.shows = shows
        for r in self.runners:
            r.camper.set_shows(shows)
        self.retarget_listeners()
        self.last_discovery = time.time()
        self.on_event(("shows", shows))
        return True

    def _still_live(self, seller: str, show_id: str):
        """True/False; raises when Whatnot could not be asked (the camper treats that as 'unknown')."""
        node = self.client.seller_live_show(seller)
        return node is not None and node.get("id") == show_id

    def refresh_now(self, wait: bool = False) -> None:
        """The Refresh button: live streams now, and every giveaway list due again."""
        self.giveaways.mark_all_stale()
        if self.running and not wait:
            self._refresh_asked.set()                # the engine loop does it on its next turn (0.2s)
            return
        if wait:
            self._discover()
        else:                                        # bot stopped: nobody else will, so do it here
            threading.Thread(target=self._discover, daemon=True).start()

    def rank_extras(self) -> dict:
        now = time.time()
        return {"listed": self.giveaways.summary(),
                "cooling": {s for s, until in list(self.cooldowns.items()) if until > now},
                "activity": self.activity_book, "now": now}

    def listen_order(self) -> list[str]:
        """Which streams the background listeners should be on: where we are, then the scanner's own order
        (pack-only first, best value first), so the streams it chooses among are the ones it knows about."""
        return self.interesting_show_ids()

    def retarget_listeners(self) -> None:
        try:
            order = self.listen_order()
            self.watcher.set_shows(self.shows, self.followed_any(), order)
            now = time.time()
            self.activity_book.watching(order[: self.cfg.whatnot.watch_slots], now)
            self.activity_book.forget(s.id for s in self.shows)
        except Exception:
            log.exception("retarget_listeners")

    def interesting_show_ids(self) -> list[str]:
        """Streams worth a giveaway-list request, best first: where we are, then the ranked list."""
        camped = [r.camper.state.camped.id for r in self.runners if r.camper.state.camped is not None]
        # keep_excluded: EVERY stream, the scanner's choices first. Seen live: lists were only fetched for
        # ranked streams, the scanner only ranked streams whose list was known, and nothing ever got started.
        # (refresh() also forgets any stream left out of this list.)
        ranked = rank_streams(self.shows, self.giveaway_counts, self.cfg, self.followed_any(),
                              self.store.blacklisted_sellers(), favorites=set(self.store.favorites()),
                              keep_excluded=True, **self.rank_extras())
        out = list(dict.fromkeys(camped + [p.show.id for p in ranked]))
        return out

    def watch_giveaway_lists(self):
        """A couple of requests every few seconds, forever. Independent of Start/Stop
        once streams are known; the directory itself decides what is stale."""
        def loop():
            while True:
                try:
                    if self.shows:
                        self.giveaways.refresh(self.interesting_show_ids())
                        if time.time() - getattr(self, "_retargeted_at", 0.0) >= 60:
                            self._retargeted_at = time.time()
                            self.retarget_listeners()
                except Exception:
                    log.exception("giveaway list refresh failed")
                time.sleep(4)
        threading.Thread(target=loop, daemon=True).start()

    def watch_adb(self):
        """Which devices are up, refreshed off the UI thread and independent of
        Start/Stop: the power buttons must work while the bot itself is stopped."""
        def loop():
            while True:
                try:
                    self.connected = list_devices(self.cfg.device.android_sdk)
                except Exception:
                    pass
                time.sleep(3)
        threading.Thread(target=loop, daemon=True).start()

    # ---- threads ----
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        for r in self.runners:
            r.thread = threading.Thread(target=self._camp_loop, args=(r,), daemon=True)
            r.thread.start()

    def stop(self):
        self._stop.set()
        for r in self.runners:
            r.wake.set()                                 # the loops wait on this now: let them see _stop
        for r in self.runners:
            self.claims.release(r.account)

    def _camp_loop(self, r: AccountRunner):
        """One account. Never dies quietly: a bad tick is logged and the loop goes on."""
        while not self._stop.is_set():
            t0 = time.time()
            try:
                if not r.camper.state.paused:
                    r.ensure_running()
                r.camper.step()
            except Exception as e:
                log.exception("step failed for %s", r.account)
                r.camper.state.note(f"step failed: {e}")
            r.last_tick = time.time()
            r.wake.wait(max(0.2, r.camper.wanted_poll_interval() - (time.time() - t0)))
            r.wake.clear()
        self.claims.release(r.account)

    def _run(self):
        """Never let the engine die quietly.

        It did once: an exception inside the loop unwound the thread, the window
        stayed up looking healthy, and nothing ran again. Any failure is now
        logged, surfaced in the UI, and the loop restarts.
        """
        while not self._stop.is_set():
            try:
                asyncio.run(self._main())
                return                      # clean stop
            except Exception as e:
                log.exception("engine crashed")
                self.note(f"engine crashed, restarting: {e}")
                self.crashes += 1
                time.sleep(3)

    async def _main(self):
        wtask = asyncio.create_task(self.watcher.run())
        last_disc = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if self._refresh_asked.is_set() or now - last_disc >= self.cfg.whatnot.discovery_interval_s:
                    self._refresh_asked.clear()
                    await asyncio.to_thread(self._discover)
                    last_disc = now
                self.last_tick = now
                await asyncio.sleep(0.2)
        finally:
            wtask.cancel()


class App(tk.Tk):
    def __init__(self, autostart: bool = False):
        super().__init__()
        self.title("Whatnot Givvy")
        self.cfg = load()
        self.q: queue.Queue = queue.Queue()
        self.engine = Engine(self.cfg, self.q.put)
        self.engine.watch_adb()
        self.engine.watch_giveaway_lists()
        self._ui_state_path = self.cfg.root / "ui_state.json"
        self._load_ui_state()
        try:                                              # did the update started last time install?
            from .updater import update_outcome
            from .version import __version__
            here = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
            msg = update_outcome(__version__, here)
            if msg:
                self.engine.note(msg)
                if "did not install" in msg:
                    from tkinter import messagebox
                    self.after(1500, lambda: messagebox.showwarning("Whatnot Givvy update", msg, parent=self))
        except Exception:
            log.exception("update outcome")
        load_modes(self._ui, self.engine)             # each account back in the mode it was left in
        self._last_held: dict = {}
        self._last_lines: list = []
        self._ready = False
        self._save_job = None
        self._set_icon()
        from . import themes
        themes.THEME_DIRS[:] = [self.cfg.root / "themes"]       # themes/<name>/: art that is not in the code
        self.theme = themes.get(self._ui.get("theme", themes.DEFAULT))
        themes.apply_style(self, self.theme)             # before building, so nothing appears in the wrong colours
        self._build()
        self.apply_theme(self.theme.name)
        self._apply_log_visibility()
        self.after(250, self._place_window)      # after the window is mapped
        self.after(REFRESH_MS, self._tick)
        self.after(1200, self._startup_check)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        if autostart:
            self.after(1500, self._start)

    # ---- window ----
    def _set_icon(self):
        try:
            self.iconbitmap(default=str(ICON))       # title bars, including dialogs
        except Exception:
            log.warning("could not set the window icon from %s", ICON)
        # The taskbar ignores that class icon once the process has its own app
        # identity, and showed pythonw's icon instead. Give the window its own.
        self.after(400, lambda: brand_window(self, ICON))

    def _place_window(self):
        """Opens maximised the first time; after that, the way you left it. It used
        to force itself back to full screen whenever it was minimised. You asked for
        that once, then asked for it to behave like a normal window, so it does."""
        self._apply_minsize()
        win = self._ui.get("window", {})
        placed = False
        if win.get("geometry") and not win.get("zoomed", True):
            try:
                self.geometry(win["geometry"])
                placed = True
            except tk.TclError:
                pass
        if not placed:
            try:
                self.state("zoomed")
            except tk.TclError:
                self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        self.lift()
        self._ready = True
        self.bind("<Configure>", self._window_changed)

    def _apply_minsize(self):
        w = 640 if self._ui.get("log_hidden") else 900
        self.minsize(w + (420 if self._ui.get("charts_shown") else 0), 480)

    def _window_changed(self, event=None):
        """Remember size/position, debounced: a drag fires hundreds of these."""
        if event is not None and event.widget is not self:
            return
        if self._save_job is not None:
            self.after_cancel(self._save_job)
        self._save_job = self.after(1200, self._remember_window)

    def _remember_window(self):
        self._save_job = None
        try:
            state = self.state()
            if state == "iconic":
                return                               # minimised: keep what we had
            win = self._ui.setdefault("window", {})
            win["zoomed"] = state == "zoomed"
            if state == "normal":
                win["geometry"] = self.geometry()
            self._save_ui_state()
        except tk.TclError:
            pass

    # activity log: collapsible, so the window can be narrow while it runs
    def _toggle_log(self):
        hide = not self._ui.get("log_hidden", False)
        self._ui["log_hidden"] = hide
        width = self.right.winfo_width() if self.right.winfo_ismapped() else self._ui.get("log_width", 600)
        if hide:
            self._ui["log_width"] = width
        self._apply_log_visibility()
        self._apply_minsize()
        try:
            if self.state() == "normal":             # a maximised window just gives the space to the lists
                w, h = self.winfo_width(), self.winfo_height()
                w = max(640, w - width) if hide else min(self.winfo_screenwidth(), w + width)
                self.geometry(f"{w}x{h}")
        except tk.TclError:
            pass
        self._save_ui_state()

    def _apply_log_visibility(self):
        if self._ui.get("log_hidden", False):
            self.right.pack_forget()
            self.log_btn.config(text="\u25c0 Show activity")
        else:
            self.right.pack(side="left", fill="both", expand=True, padx=(10, 0), after=self.favs_frame)
            self.log_btn.config(text="Hide activity \u25b6")
        self._apply_charts_visibility()

    # Win Charts: expands out to the right, like the activity log
    def _toggle_charts(self):
        show = not self._ui.get("charts_shown", False)
        self._ui["charts_shown"] = show
        width = self.charts.winfo_width() if self.charts.winfo_ismapped() else self._ui.get("charts_width", 560)
        if not show:
            self._ui["charts_width"] = width
        self._apply_charts_visibility()
        self._apply_minsize()
        try:
            if self.state() == "normal":             # a maximised window just shares out the space it has
                w, h = self.winfo_width(), self.winfo_height()
                w = min(self.winfo_screenwidth(), w + width) if show else max(640, w - width)
                self.geometry(f"{w}x{h}")
        except tk.TclError:
            pass
        self._save_ui_state()

    def _apply_charts_visibility(self):
        if not hasattr(self, "charts"):
            return
        if self._ui.get("charts_shown", False):
            # Laid out FIRST, at the right edge: whatever is laid out last is squeezed when the window is narrow,
            # and at 1900 px that was the whole charts panel. The stream list gives way instead.
            self.charts.pack(side="right", fill="both", expand=True, padx=(10, 0), before=self.streams_frame)
            self.charts_btn.config(text="Hide Win Charts \u25b6")
            self.after(50, self.charts.redraw)
        else:
            self.charts.pack_forget()
            self.charts_btn.config(text="\u25c0 Win Charts")

    def _build(self):
        pad = dict(padx=10, pady=6)
        # Bottom-left corner: which version is this? Packed first so it keeps the bottom edge.
        from .version import short_version
        foot = ttk.Frame(self); foot.pack(side="bottom", fill="x", padx=10, pady=(0, 4))
        ttk.Label(foot, text=f"Whatnot Givvy {short_version()}", style="Faint.TLabel",
                  font=("Segoe UI", 8)).pack(side="left")
        from .themes import available
        ttk.Label(foot, text="Theme:", style="Faint.TLabel", font=("Segoe UI", 8)).pack(side="left", padx=(18, 4))
        self.theme_box = ttk.Combobox(foot, values=available(), state="readonly", width=16)
        self.theme_box.set(self.theme.name)
        self.theme_box.bind("<<ComboboxSelected>>", lambda _e: self.apply_theme(self.theme_box.get()))
        self.theme_box.pack(side="left")
        self.blast_btn = ttk.Button(foot, text="Test the blast", command=self._test_blast)   # themes with a celebration
        self.credit_lbl = ttk.Label(foot, text="", style="Faint.TLabel", font=("Segoe UI", 7))  # e.g. Riot's, for Nunu
        top = ttk.Frame(self); top.pack(fill="x", **pad)

        self.start_btn = ttk.Button(top, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(top, text="Pause", command=self._toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=6)
        self.charts_btn = ttk.Button(top, text="\u25c0 Win Charts", width=18, command=self._toggle_charts)
        self.charts_btn.pack(side="right")
        self.log_btn = ttk.Button(top, text="Hide activity \u25b6", width=18, command=self._toggle_log)
        self.log_btn.pack(side="right", padx=(0, 6))
        ttk.Button(top, text="Settings...", command=self._open_settings).pack(side="right", padx=8)
        self.status = ttk.Label(top, text="stopped", font=("Segoe UI", 10))
        self.status.pack(side="left", padx=16)
        # Big, in the middle of the top bar: today's wins, updated on the next refresh after a win (under a second).
        centre = ttk.Frame(top); centre.pack(side="left", expand=True)
        self.logo_lbl = ttk.Label(centre)                 # the Time Bomb TCG theme's logo; empty otherwise
        self.today_lbl = ttk.Label(centre, text="Total Wins Today: 0", style="Today.TLabel")
        self.today_lbl.pack(side="left")
        self._today_shown = None
        self._today_flash = None

        # ---- devices: one row each; devices sharing an account are alternatives ----
        devs = ttk.LabelFrame(self, text="Devices  (same account = alternatives: the first one ticked and "
                                         "connected runs.  Two accounts are never put in the same stream.)")
        devs.pack(fill="x", **pad)
        self.devs = devs
        self._build_device_rows()

        mid = ttk.Frame(self); mid.pack(fill="both", expand=True, **pad)

        left = ttk.LabelFrame(mid, text="Live streams")
        left.pack(side="left", fill="both", expand=True)
        self.streams_frame = left
        pinbar = ttk.Frame(left); pinbar.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(pinbar, text="Double-click a stream to sit in it with account:").pack(side="left")
        names = [r.account for r in self.engine.runners]
        self.pin_var = tk.StringVar(value=names[0] if names else "")
        self.pin_box = ttk.Combobox(pinbar, textvariable=self.pin_var, values=names, state="readonly", width=14)
        self.pin_box.pack(side="left", padx=6)
        self.tree = ttk.Treeview(left, columns=("game", "viewers", "givvies", "score", "held", "prizes"),
                                 show="tree headings", height=18)
        self.tree.heading("#0", text="Seller")
        for c, w, label in (("game", 90, "Game"), ("viewers", 80, "Viewers"), ("givvies", 80, "Givvies"),
                            ("score", 150, "Kind / wins per hour"), ("held", 110, "Held by")):
            self.tree.heading(c, text=label); self.tree.column(c, width=w, anchor="center")
        self.tree.column("#0", width=250, stretch=False)
        for c in ("game", "viewers", "givvies", "score", "held"):
            self.tree.column(c, stretch=False)
        self.tree.heading("prizes", text="Giveaways listed  (×N = how many are left)", anchor="w")
        self.tree.column("prizes", width=300, anchor="w", stretch=True)   # grows with the window
        self.prize_lbl = ttk.Label(left, text="", wraplength=900, justify="left", style="Body.TLabel")
        self.prize_lbl.pack(side="bottom", fill="x", padx=8, pady=(0, 6))
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree.bind("<Double-1>", self._pick_stream)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_selected_prizes())
        self.tree.bind("<<TreeviewOpen>>", self._band_toggled)
        self.tree.bind("<<TreeviewClose>>", self._band_toggled)
        left.bind("<Configure>", lambda e: self.prize_lbl.config(wraplength=max(300, e.width - 40)))
        ttk.Button(pinbar, text="\u2605 Favorite the selected stream", command=self._fav_selected).pack(side="right")
        self.refresh_btn = ttk.Button(pinbar, text="\u21bb Refresh", command=self._refresh_streams)
        self.refresh_btn.pack(side="right", padx=8)
        self.updated_lbl = ttk.Label(pinbar, text="", style="Muted.TLabel")
        self.updated_lbl.pack(side="right")

        favs = ttk.LabelFrame(mid, text="Favorites  (double-click a live one to sit in it)")
        favs.pack(side="left", fill="y", padx=(10, 0))
        self.fav_tree = ttk.Treeview(favs, columns=("status", "held"), show="tree headings", height=18)
        self.fav_tree.heading("#0", text="Streamer")
        self.fav_tree.heading("status", text="Now"); self.fav_tree.heading("held", text="Held by")
        self.fav_tree.column("#0", width=190); self.fav_tree.column("status", width=130, anchor="center")
        self.fav_tree.column("held", width=80, anchor="center")
        self.fav_tree.pack(fill="both", expand=True, padx=6, pady=6)
        self.fav_tree.bind("<Double-1>", self._pick_favorite)
        favbar = ttk.Frame(favs); favbar.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(favbar, text="Add by name...", command=self._fav_by_name).pack(side="left")
        ttk.Button(favbar, text="Remove", command=self._fav_remove).pack(side="left", padx=6)
        self._fill_favs()

        self.favs_frame = favs
        self.right = ttk.LabelFrame(mid, text="Activity (all accounts)")
        self.log = tk.Text(self.right, height=20, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        from .wincharts import WinChartsPanel
        self.charts = WinChartsPanel(mid, self.engine.store, accounts=lambda: [r.account for r in self.engine.runners])

    def _build_device_rows(self):
        """(Re)draw the device table; called again when an emulator account is added."""
        devs = self.devs
        for w in devs.winfo_children():
            w.destroy()
        heads = ("Use", "Device", "Account", "Mode", "Emulator power / rename", "State", "Stream", "Entries/hr", "", "Last action")
        for c, h in enumerate(heads):
            ttk.Label(devs, text=h, font=("Segoe UI", 9, "bold")).grid(row=0, column=c, sticky="w", padx=8, pady=(4, 2))
        devs.columnconfigure(9, weight=1)
        self.rows = []
        self.power_btns = {}
        self.blacklist_btns = {}
        self.mode_boxes = []                                 # (runner, combobox)
        n = 1
        for r in self.engine.runners:
            r.on_enabled_changed = self._save_ui_state
            for d in r.devices:
                var = tk.BooleanVar(value=r.enabled.get(d.name, True))
                ttk.Checkbutton(devs, variable=var,
                                command=lambda r=r, d=d, var=var: self._device_toggled(r, d, var)
                                ).grid(row=n, column=0, padx=8)
                ttk.Label(devs, text=f"{d.name}  ({d.serial})").grid(row=n, column=1, sticky="w", padx=8)
                ttk.Label(devs, text=r.account).grid(row=n, column=2, sticky="w", padx=8)
                mode_box = ttk.Combobox(devs, values=list(MODES.values()), state="readonly", width=17)
                mode_box.set(MODES.get(r.camper.state.mode, MODES["scan"]))
                mode_box.bind("<<ComboboxSelected>>", lambda e, r=r, b=mode_box: self._row_mode_changed(r, b))
                mode_box.grid(row=n, column=3, sticky="w", padx=8)
                self.mode_boxes.append((r, mode_box))
                box = ttk.Frame(devs); box.grid(row=n, column=4, sticky="w", padx=8)
                ttk.Button(box, text="Rename", width=8,
                           command=lambda d=d: self._rename(d.name)).pack(side="right", padx=(8, 0))
                if hasattr(d, "power_off"):                  # emulators only; a phone is yours to power
                    btns = []
                    for text, action in (("Start", "start"), ("Restart", "restart"), ("Power off", "off")):
                        b = ttk.Button(box, text=text, width=9,
                                       command=lambda r=r, d=d, a=action: self._power(r, d, a))
                        b.pack(side="left", padx=1)
                        btns.append(b)
                    self.power_btns[d.name] = btns
                labels = [ttk.Label(devs, text="") for _ in range(4)]
                for c, lab in zip((5, 6, 7, 9), labels):
                    lab.grid(row=n, column=c, sticky="w", padx=8)
                bl = ttk.Button(devs, text="Blacklist stream", width=15, state="disabled",
                                command=lambda r=r: self._blacklist_current(r))
                bl.grid(row=n, column=8, sticky="w", padx=8)
                self.blacklist_btns[d.name] = bl
                self.rows.append((r, d, var, labels))
                n += 1

    # ---- controls ----
    def _load_ui_state(self):
        """Tick-boxes, window placement and whether the activity log is shown."""
        try:
            self._ui = json.loads(self._ui_state_path.read_text(encoding="utf-8"))
            if not isinstance(self._ui, dict):
                self._ui = {}
        except Exception:
            self._ui = {}
        saved = self._ui.get("enabled", {})
        for r in self.engine.runners:
            for d in r.devices:
                if d.name in saved:
                    r.enabled[d.name] = bool(saved[d.name])

    def _save_ui_state(self):
        try:
            self._ui["enabled"] = {d.name: r.enabled.get(d.name, True)
                                   for r in self.engine.runners for d in r.devices}
            self._ui_state_path.write_text(json.dumps(self._ui, indent=1), encoding="utf-8")
        except Exception:
            log.exception("could not save ui state")

    def _open_settings(self, tab: str = ""):
        """Emulator resources, pinning, the bot's numbers, adding an emulator: all
        the things that do not belong on the main screen."""
        from .settings_window import SettingsWindow
        win = getattr(self, "_settings", None)
        if win is not None and win.winfo_exists():
            win.lift(); win.focus_set()
            if tab == "check":
                win.nb.select(win.check_tab)
            return
        self._settings = SettingsWindow(self, tab)
        self.dress(self._settings)

    def _startup_check(self):
        """A fresh install has no emulator yet, and a PC that has never run one usually
        has Windows Hypervisor Platform off. Either way Start would do nothing visible,
        which is exactly what a friend hit. Look first, and open the repair page."""
        def work():
            try:
                from . import doctor
                avds = [d.avd for r in self.engine.runners for d in r.devices if hasattr(d, "avd")]
                if not avds:
                    return
                bad = doctor.first_problem(doctor.run(self.cfg.device.android_sdk, avds))
                if bad is not None:
                    self.engine.note(f"emulator check: {bad.title} - NOT OK. Opening Settings > Emulator check.")
                    self.q.put(("open_check", None))      # Tk must only be touched from its own thread
            except Exception:
                log.exception("startup emulator check failed")
        threading.Thread(target=work, daemon=True).start()

    def _add_emulator(self):
        """Another account = another emulator. Creates it; you sign in to it."""
        from tkinter import messagebox, simpledialog
        from .accounts import add_emulator_to_config, create_avd
        name = simpledialog.askstring("Add emulator account",
                                      "Short name for the new emulator (letters/digits), e.g. emu2:\n\n"
                                      "It takes about 4 GB of disk and 6 GB of RAM while running.\n"
                                      "Accounts are never put in the same stream as each other.", parent=self)
        if not name:
            return
        try:
            new = add_emulator_to_config(self.cfg.root / "config.toml", name.strip())
        except Exception as e:
            messagebox.showerror("Whatnot Givvy", str(e))
            return
        self.engine.note(f"creating emulator {new.name} (a minute or so)...")

        def work():
            try:
                create_avd(self.cfg.device.android_sdk, new.avd)
                self.q.put(("call", lambda: self._emulator_added(new)))      # back on the UI thread
            except Exception as e:
                self.engine.note(f"creating {new.name} failed: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _rename(self, device_name: str):
        """Two names: what the device is called, and its account label (the
        Whatnot login; phone + emulator on the same login share one label)."""
        from tkinter import messagebox
        runner = next((r for r in self.engine.runners if any(d.name == device_name for d in r.devices)), None)
        if runner is None:
            return
        win = tk.Toplevel(self)
        win.title("Rename")
        win.transient(self); win.grab_set(); win.resizable(False, False)
        frm = ttk.Frame(win, padding=14); frm.pack()
        name_var, acc_var = tk.StringVar(value=device_name), tk.StringVar(value=runner.account)
        ttk.Label(frm, text="Device name:").grid(row=0, column=0, sticky="w", pady=4)
        first = ttk.Entry(frm, textvariable=name_var, width=28); first.grid(row=0, column=1, padx=8)
        ttk.Label(frm, text="Account label:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=acc_var, width=28).grid(row=1, column=1, padx=8)
        shared = [d.name for d in runner.devices if d.name != device_name]
        hint = ("Letters, digits, - and _ only. The account label is the Whatnot login: it tags the log and "
                "Discord lines and keeps the entry history."
                + (f"\nAlso on this account: {', '.join(shared)} (the label changes for them too)." if shared else ""))
        ttk.Label(frm, text=hint, wraplength=380, style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=6)
        self.dress(win)

        def apply(_e=None):
            try:
                self.engine.rename(device_name, name_var.get().strip(), acc_var.get().strip())
            except ValueError as e:
                messagebox.showerror("Rename", str(e), parent=win)
                return
            win.destroy()
            self._build_device_rows()
            names = [r.account for r in self.engine.runners]
            self.pin_box.config(values=names)
            if self.pin_var.get() not in names:
                self.pin_var.set(names[0])
            self._save_ui_state()
            self._fill_tree(self.engine.shows); self._fill_favs()

        btns = ttk.Frame(frm); btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Rename", command=apply).pack(side="right", padx=6)
        win.bind("<Return>", apply); win.bind("<Escape>", lambda _e: win.destroy())
        first.focus_set(); first.select_range(0, "end")

    def _emulator_added(self, new):
        from tkinter import messagebox
        self.engine.add_account(new)
        self._build_device_rows()
        self.pin_box.config(values=[r.account for r in self.engine.runners])
        self._save_ui_state()
        self.engine.note(f"emulator {new.name} added. Press its Start button, install Whatnot in it and sign in.")
        messagebox.showinfo("Whatnot Givvy",
                            f"'{new.name}' is in the device list now.\n\n"
                            "1. Press its Start button (or it boots by itself while the bot is running).\n"
                            "2. In the emulator: sign in to Play Store, install Whatnot, sign in to Whatnot.\n\n"
                            "The bot never types passwords. Until Whatnot is installed the row says so and does nothing.")

    def _device_toggled(self, runner, device, var):
        runner.set_enabled(device.name, var.get())
        self._save_ui_state()
        self.engine.note(f"{device.name} {'enabled' if var.get() else 'disabled'}")

    def _row_mode_changed(self, runner, box):
        mode = next((k for k, v in MODES.items() if v == box.get()), "scan")
        self.engine.set_mode(runner.account, mode)
        self._save_modes()

    def _save_modes(self):
        save_modes(self._ui, self.engine)
        self._save_ui_state()

    def _blacklist_current(self, runner):
        show = runner.camper.state.camped
        if show is None:
            return
        from tkinter import messagebox
        if not messagebox.askyesno("Blacklist stream",
                                   f"Blacklist {show.seller}?\n\nThe scanner will never pick this seller again, on any "
                                   f"account, and {runner.account} moves to another stream now.\n\n"
                                   "Undo: Settings... > Blacklist.", parent=self):
            return
        if runner.camper.state.camped is not show:           # it moved while the question was open
            self.engine.note(f"{runner.account} had already left {show.seller}; nothing blacklisted")
            return
        runner.camper.blacklist_current()
        self.engine.notifier.plain(f"Blacklisted {show.seller} from the scanner: by hand")
        self._fill_tree(self.engine.shows)

    def _refresh_streams(self):
        self.engine.note("refreshing live streams and giveaway lists")
        self.refresh_btn.config(state="disabled")
        self.after(4000, lambda: self.refresh_btn.config(state="normal"))     # no hammering Whatnot by double-click
        self.engine.refresh_now()

    def _power(self, runner, device, action: str):
        self.engine.note(f"{device.name}: {runner.POWER[action]}")
        runner.power(device.name, action)

    # favorites
    def _fav_selected(self):
        sel = self.tree.selection()
        show = next((s for s in self.engine.shows if sel and s.id == sel[0]), None)
        if show is not None:
            self.engine.add_favorite(show.seller)
            self._fill_favs(); self._fill_tree(self.engine.shows)

    def _fav_by_name(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("Add favorite", "Whatnot username of the streamer:", parent=self)
        if name:
            self.engine.add_favorite(name)
            self._fill_favs(); self._fill_tree(self.engine.shows)

    def _fav_remove(self):
        for seller in self.fav_tree.selection():
            self.engine.remove_favorite(seller)
        self._fill_favs(); self._fill_tree(self.engine.shows)

    def _pick_favorite(self, _e):
        sel = self.fav_tree.selection()
        show = next((s for s in self.engine.shows if sel and s.seller.lower() == sel[0]), None)
        if show is None:
            if sel:
                self.engine.note(f"{sel[0]} is not live right now")
            return
        self._sit_in(show)

    def _fill_favs(self):
        eng = self.engine
        live = {s.seller.lower(): s for s in eng.shows}
        held = eng.claims.snapshot()
        sel = self.fav_tree.selection()
        self.fav_tree.delete(*self.fav_tree.get_children())
        favs = eng.store.favorites()
        for seller in sorted(favs, key=lambda f: (f not in live, f)):        # live ones first
            s = live.get(seller)
            self.fav_tree.insert("", "end", iid=seller, text=seller,
                                 values=(f"LIVE  {s.viewers} watching" if s else "offline",
                                         held.get(s.id, "") if s else ""))
        if sel and self.fav_tree.exists(sel[0]):
            self.fav_tree.selection_set(sel[0])

    def _band_toggled(self, _e=None):
        """Remember which bands are open. Fires on the expander arrow and on double-click."""
        self._ui["bands"] = {iid[5:]: bool(self.tree.item(iid, "open")) for iid in self.tree.get_children()}
        self._save_ui_state()

    def _pick_stream(self, _e):
        sel = self.tree.selection()
        if not sel:
            return
        if sel[0].startswith("band:"):
            self.tree.item(sel[0], open=not self.tree.item(sel[0], "open"))
            self._band_toggled()
            return
        show = next((s for s in self.engine.shows if s.id == sel[0]), None)
        if show is not None:
            self._sit_in(show)

    def _sit_in(self, show):
        runner = self.engine.runner(self.pin_var.get())
        if runner is None:
            return
        holder = self.engine.claims.holder(show.id)
        if holder is not None and holder != runner.account:
            # one entry per person per giveaway: never two of our accounts in one stream
            self.engine.note(f"{show.seller} is held by {holder}; {runner.account} will not join it")
            return
        runner.camper.set_mode("single")
        runner.camper.pin(show.url)
        self._save_modes()

    def _start(self):
        self.engine.start()
        self.start_btn.config(text="Stop", command=self._stop)
        self.pause_btn.config(state="normal")

    def _stop(self):
        self.engine.stop()
        self.start_btn.config(text="Start", command=self._start)
        self.pause_btn.config(state="disabled")

    def _toggle_pause(self):
        paused = not all(r.camper.state.paused for r in self.engine.runners)
        for r in self.engine.runners:
            r.camper.pause(paused)
        self.pause_btn.config(text="Resume" if paused else "Pause")

    # ---- refresh ----
    def _tick(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "shows":
                    self._fill_tree(payload)
                    self._fill_favs()
                elif kind == "open_check":
                    self._open_settings("check")
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        eng, now = self.engine, time.time()
        self._refresh_prizes()
        if eng.last_discovery:
            every = int(self.cfg.whatnot.discovery_interval_s)
            nxt = max(0, int(eng.last_discovery + every - now))
            auto = f"next in {nxt}s" if eng.running else "bot stopped: press Refresh"
            known, total = eng.giveaways.coverage([s.id for s in eng.shows])
            self.updated_lbl.config(text=f"{len(eng.shows)} live, checked {time.strftime('%H:%M:%S', time.localtime(eng.last_discovery))}  "
                                         f"({auto})   giveaway lists: {known}/{total}"
                                         + (f"   {eng.discovery.hidden_abroad} outside {eng.discovery.home_country} hidden"
                                            if eng.discovery.hidden_abroad else ""))
        else:
            self.updated_lbl.config(text="not checked yet")
        held = eng.claims.snapshot()
        if held != self._last_held:                  # keep the 'held by' column honest
            self._last_held = held
            self._fill_tree(eng.shows)
            self._fill_favs()

        for r_, box_ in self.mode_boxes:
            want = MODES.get(r_.camper.state.mode, MODES["scan"])
            if box_.get() != want:
                box_.set(want)
        for runner, dev, var, (state_l, stream_l, cap_l, action_l) in self.rows:
            st = runner.camper.state
            if var.get() != runner.enabled.get(dev.name, True):      # a power button ticked/unticked it
                var.set(runner.enabled.get(dev.name, True))
            busy = runner.busy.get(dev.name)
            for b in self.power_btns.get(dev.name, ()):
                b.config(state="disabled" if busy else "normal")
            active = eng.running and runner._cached is dev and not busy
            if dev.name in self.blacklist_btns:              # only when this row is the one sitting in a stream
                self.blacklist_btns[dev.name].config(state="normal" if (active and st.camped) else "disabled")
            if busy:
                state = busy
            elif not var.get():
                state = "off" + ("  (still running)" if eng.connected.get(dev.serial) else "")
            elif runner.is_booting(dev.name):
                state = "booting..."
            elif dev.name in runner.problems and eng.connected.get(dev.serial) != "device":
                state = "CANNOT START"
            elif active:
                state = ("NEEDS SIGN-IN" if st.paused_reason == "login" else "PAUSED") if st.paused else f"ACTIVE: {st.stage}"
            elif eng.connected.get(dev.serial) == "device":
                state = "connected (standby)" if eng.running else "connected"
            else:
                state = "not connected"
            state_l.config(text=state)
            if active:
                if st.camped:
                    mins = (now - st.camped_since) / 60 if st.camped_since else 0
                    stream_l.config(text=f"{st.camped.seller}  ({st.camped.game}, {mins:.0f} min, "
                                         f"{st.entered_here} entered)")
                else:
                    stream_l.config(text="not in a stream")
                cap_l.config(text=f"{eng.store.entries_in_last_hour(now, runner.account)}"
                                  f" / {self.cfg.pacing.max_entries_per_hour}")
                late = f"   STALLED {int(now - runner.last_tick)}s" if runner.last_tick and now - runner.last_tick > 90 else ""
                action_l.config(text=(st.last_action + late)[:110])
            elif dev.name in runner.problems and eng.connected.get(dev.serial) != "device":
                why, _fix = runner.problems[dev.name]
                stream_l.config(text=""); cap_l.config(text="")
                action_l.config(text=(why + "   ->  Settings... > Emulator check")[:200])
            else:
                stream_l.config(text=""); cap_l.config(text=""); action_l.config(text="")

        stalled = ""
        if eng.running and eng.last_tick and now - eng.last_tick > 30:
            stalled = f"   |   ENGINE STALLED {int(now - eng.last_tick)}s"
        self.status.config(text=f"{'running' if eng.running else 'stopped'}   |   "
                                f"{len(eng.runners)} account(s), {len(self.rows)} device(s){stalled}")
        lines = list(eng.activity)[-60:]
        if lines != self._last_lines:
            self._last_lines = lines
            self.log.config(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", "\n".join(reversed(lines)))
            self.log.config(state="disabled")
        if self._ui.get("charts_shown", False):
            try:
                self.charts.refresh()
            except Exception:
                log.exception("win charts")
        self._show_wins_today()
        self.after(REFRESH_MS, self._tick)

    def _show_wins_today(self):
        """'Total Wins Today: N'. Green for a few seconds when it goes up."""
        try:
            from .wincharts import wins_today
            n = wins_today(self.engine.store)
        except Exception:
            log.exception("wins today")
            return
        if n == self._today_shown:
            return
        went_up = self._today_shown is not None and n > self._today_shown
        self._today_shown = n
        self.today_lbl.config(text=f"Total Wins Today: {n}")
        if went_up and self.theme.celebration:
            self._celebrate()
        if went_up:
            if self._today_flash is not None:
                self.after_cancel(self._today_flash)
            self.today_lbl.config(style="TodayFlash.TLabel")
            self._today_flash = self.after(FLASH_MS, self._end_flash)

    def _celebrate(self, caption: str | None = None):
        """The theme's celebration: Time Bomb TCG's bomb, or Nunu & Willump's snowball. '<account> Won <prize>'."""
        try:
            from . import themes
            from .blast import Blast, caption_for
            if caption is None:
                last = self.engine.store.latest_win()
                caption = caption_for(*last) if last else ""
            if self.theme.celebration == "snowball":
                from .snowball import Snowball
                folder = themes.asset_dir(self.theme)
                if folder is not None:
                    Snowball(self, caption, folder).start()
            else:
                Blast(self, caption).start()
        except Exception:
            log.exception("celebration")

    def _test_blast(self):
        from .blast import caption_for
        last = self.engine.store.latest_win()
        self._celebrate(caption_for(*last) if last else "jgoblin22 Won Free Pack Givey #17")

    def _end_flash(self):
        self._today_flash = None
        self.today_lbl.config(style="Today.TLabel")

    # ---- themes: Light, Dark, Time Bomb TCG (Settings-free: the picker is in the bottom bar)
    def apply_theme(self, name: str):
        from . import themes
        t = themes.apply(self, name)
        self.theme = t
        self._ui["theme"] = t.name
        if hasattr(self, "charts"):
            self.charts.set_theme(t)
        if hasattr(self, "logo_lbl"):
            self._show_logo(themes.logo_path(t))
        if hasattr(self, "blast_btn"):
            if t.celebration:
                self.blast_btn.config(text="Test the snowball" if t.celebration == "snowball" else "Test the blast")
                self.blast_btn.pack(side="left", padx=(8, 0))
            else:
                self.blast_btn.pack_forget()
        if hasattr(self, "credit_lbl"):
            if t.credit:
                self.credit_lbl.config(text=t.credit)
                self.credit_lbl.pack(side="left", padx=(12, 0))
            else:
                self.credit_lbl.pack_forget()
        if hasattr(self, "theme_box") and self.theme_box.get() != t.name:
            self.theme_box.set(t.name)
        self._save_ui_state()

    def dress(self, win):
        """A window opened after the theme was chosen: its title bar and plain Tk parts."""
        from . import themes
        themes.dress(win, self.theme)

    def _show_logo(self, path):
        show = path is not None
        if show and getattr(self, "_logo_path", None) != path:
            try:
                self._logo_img = tk.PhotoImage(file=str(path))
                self._logo_path = path
            except tk.TclError:
                log.warning("could not load %s", path)
                show = False
        if show:
            self.logo_lbl.config(image=self._logo_img)
            self.logo_lbl.pack(side="left", padx=(0, 12), before=self.today_lbl)
        else:
            self.logo_lbl.pack_forget()

    def _fill_tree(self, shows):
        eng = self.engine
        favs = set(eng.store.favorites())
        ranked = rank_streams(shows, eng.giveaway_counts, self.cfg, eng.followed_any(),
                              eng.store.blacklisted_sellers(), favorites=favs, keep_excluded=True, **eng.rank_extras())
        held = eng.claims.snapshot()
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        open_bands = self._ui.get("bands", {label: i < OPEN_BY_DEFAULT for i, (_lo, label) in enumerate(BANDS)})
        groups: dict[str, list] = {label: [] for _lo, label in BANDS}
        for p in ranked:                             # ranked order is kept inside each band
            groups[band_of(p.show.viewers)].append(p)
        for _lo, label in BANDS:
            picks = groups[label]
            if not picks:
                continue
            packs = sum(1 for p in picks if not p.excluded and p.pack)
            self.tree.insert("", "end", iid="band:" + label, text=f"{label}   ({len(picks)} streams)",
                             values=("", "", "", f"{packs} packs-only" if packs else "", "", ""),
                             open=bool(open_bands.get(label, False)))
            for p in picks:
                s = p.show
                self.tree.insert("band:" + label, "end", iid=s.id,
                                 text=("\u2605 " if s.seller.lower() in favs else "") + s.seller,
                                 values=(s.game, s.viewers, eng.giveaway_counts.get(s.id, 0),
                                         p.excluded or f"{KIND_LABEL.get(p.kind, p.kind)}  {p.score:.2f}",
                                         held.get(s.id, ""), eng.giveaways.text(s.id)))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        self._prize_version = eng.giveaways.version

    def _refresh_prizes(self):
        """Giveaway lists trickle in a few per round. Update the cells in place:
        rebuilding the list every few seconds would throw the scroll position away."""
        eng = self.engine
        if getattr(self, "_prize_version", -1) == eng.giveaways.version:
            return
        self._prize_version = eng.giveaways.version
        for band in self.tree.get_children():
            for iid in self.tree.get_children(band):
                text = eng.giveaways.text(iid)
                if self.tree.set(iid, "prizes") != text:
                    self.tree.set(iid, "prizes", text)
        self._show_selected_prizes()

    def _show_selected_prizes(self):
        sel = self.tree.selection()
        show = next((s for s in self.engine.shows if sel and s.id == sel[0]), None)
        if show is None:
            self.prize_lbl.config(text="")
            return
        text = self.engine.giveaways.text(show.id, full=True) or "giveaway list not fetched yet"
        self.prize_lbl.config(text=f"{show.seller}:  {text}")

    def _quit(self):
        self.engine.stop()
        self.destroy()


def _single_instance(cfg) -> bool:
    """Refuse to start twice. Two copies drive the same phone and fight over it,
    which produced interleaved, useless runs during testing."""
    import socket
    _single_instance.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        import os
        _single_instance.sock.bind(("127.0.0.1", int(os.environ.get("GIVVY_LOCK_PORT", "47615"))))
        _single_instance.sock.listen(1)
        return True
    except OSError:
        return False


def _first_run_config() -> None:
    """The installer only copies the program. Settings are created here, from the
    shipped example, the first time the app starts."""
    from .sdk_setup import fresh_config
    example = Path("config.example.toml")
    if not example.exists():
        example = Path(__file__).resolve().parent.parent / "config.example.toml"
    sdk = Path("C:/Android/sdk")                 # no spaces, no accents: Google's emulator misbehaves otherwise
    try:
        sdk.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        import os
        sdk = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WhatnotGivvy" / "sdk"
    Path("config.toml").write_text(fresh_config(example.read_text(encoding="utf-8"), sdk), encoding="utf-8")


def main():
    from .main import setup_logging
    if "--uninstall-cleanup" in sys.argv:
        import os
        if getattr(sys, "frozen", False):
            os.chdir(Path(sys.executable).resolve().parent)
        from .uninstall import main as cleanup
        sys.exit(cleanup(sys.argv[1:]))
    if "--check-reader" in sys.argv:
        # Support: time the persistent screen reader on every connected device; reader_check.txt beside the exe.
        import statistics
        from .config import load as _load
        from .device.adb import list_devices
        from .device.reader import FastReader
        here = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
        import faulthandler
        import logging as _logging
        _hang = open(here / "reader_check_hang.txt", "w", encoding="utf-8")
        faulthandler.dump_traceback_later(90, exit=True, file=_hang)      # where it stopped, if it ever hangs
        _logging.basicConfig(filename=str(here / "reader_check.log"), level=_logging.DEBUG, force=True)
        cfg = _load(here / "config.toml")
        adb_exe = str(Path(cfg.device.android_sdk) / "platform-tools" / "adb.exe")
        lines = []
        for serial, state in sorted(list_devices(cfg.device.android_sdk).items()):
            if state != "device":
                continue
            r, times = FastReader(serial, adb_exe), []
            for _ in range(5):
                t0 = time.time(); ok = r.dump() is not None; times.append(time.time() - t0)
            lines.append(f"{serial}: {'helper works' if ok else 'HELPER FAILED'}; read {statistics.median(times):.2f}s "
                         f"(first {times[0]:.2f}s includes starting it)")
        text = "\n".join(lines) or "no device connected"
        (here / "reader_check.txt").write_text(text + "\n", encoding="utf-8")
        print(text)
        sys.exit(0)
    if "--report" in sys.argv:
        # Results per account, as text: printed from source, and written to results.txt beside the exe.
        from . import metrics
        from .config import load as _load
        here = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
        cfg = _load(here / "config.toml")
        store = Store(cfg.root / cfg.paths.db)
        now = time.time()
        text = "\n\n".join(metrics.format_report(metrics.report(store, since=now - s), label)
                           for label, s in (("Last hour", 3600), ("Last 24 hours", 86400), ("Last 7 days", 7 * 86400)))
        (here / "results.txt").write_text(text + "\n", encoding="utf-8")
        print(text)
        sys.exit(0)
    if "--check-update" in sys.argv:
        from .updater import diagnose
        here = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
        sys.exit(diagnose(here / "update_check.txt"))
    if getattr(sys, "frozen", False):
        # installed build: config, database and logs live beside the .exe
        import os
        os.chdir(Path(sys.executable).resolve().parent)
    elif not Path("config.toml").exists():
        import os
        os.chdir(Path(__file__).resolve().parent.parent)      # started from a pinned taskbar button: no working dir
    if not Path("config.toml").exists():
        try:
            _first_run_config()
        except Exception as e:
            import tkinter.messagebox as mb
            r = tk.Tk(); r.withdraw()
            mb.showerror("Whatnot Givvy", f"Could not create config.toml next to the program:\n{e}")
            return
    cfg = load()
    setup_logging(str(cfg.root / cfg.paths.log))
    set_process_app_id()                         # taskbar identity: ours, not Python's
    if not _single_instance(cfg):
        try:
            from tkinter import messagebox
            r = tk.Tk(); r.withdraw()
            messagebox.showinfo("Whatnot Givvy", "It is already running.")
            r.destroy()
        except Exception:
            pass
        return
    App(autostart="--start" in sys.argv).mainloop()


if __name__ == "__main__":
    main()
