"""Main decision loop. `step()` is synchronous and testable; `run()` wires it into asyncio."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .models import GiveawayEvent
from .notify import format_entry, format_status
from .scorer import rank, score_confirmed
from .store import Store

log = logging.getLogger(__name__)

MAX_EXPANDS = 4          # the expanded card re-collapses; retry a few times


@dataclass
class Holding:
    event: GiveawayEvent
    since: float
    device_name: str

    @property
    def live_product_id(self) -> str:
        return self.event.live_product_id


class Orchestrator:
    def __init__(self, cfg: Config, store: Store, watcher, notifier, device_getter: Callable[[], object | None],
                 followed: set[str]):
        self.cfg = cfg
        self.store = store
        self.watcher = watcher
        self.notifier = notifier
        self.device_getter = device_getter
        self.followed = set(followed) | set(store.followed_sellers())
        self.paused = False
        self.holding: Holding | None = None
        self.idle_until = 0.0
        self.entries_by_id: dict[str, int] = {}
        self._unknown_screen_strikes = 0
        Path(cfg.paths.screens_dir).mkdir(parents=True, exist_ok=True)

    # ---- one decision tick ----
    def step(self, now: float | None = None):
        now = time.time() if now is None else now
        device = self.device_getter()
        if self.holding is not None:
            self._hold_step(device, now)
            return
        if self.paused or device is None or now < self.idle_until:
            return
        if device.is_user_busy():
            return
        if self.store.entries_in_last_hour(now) >= self.cfg.pacing.max_entries_per_hour:
            return
        active = [e for e in self.watcher.active_events() if not self.store.already_tried(e.live_product_id)]
        ranked = rank(active, self.entries_by_id, self.cfg.scoring, self.followed)
        if not ranked:
            return
        cand = ranked[0]
        log.info("chasing %s (%s) score %.1f parts %s", cand.event.prize_text or "?", cand.event.show.seller, cand.score, cand.parts)
        self.store.record_giveaway(cand.event.live_product_id, cand.event.show, cand.event.listing, cand.event.started_at, cand.score)
        self._chase(device, cand.event, cand.score, now)

    def _chase(self, device, event: GiveawayEvent, score: float, now: float):
        try:
            device.open_show(event.show.url)
            deadline = now + self.cfg.pacing.enter_timeout_s
            confirmed = False
            expands = 0
            while True:
                s = device.giveaway_state()
                log.debug("chase %s: card=%s entered=%s entries=%s prize=%r follow=%s expands=%d",
                          event.show.seller, s.card_visible, s.entered, s.entries, s.prize,
                          s.follow_required, expands)
                if s.login_needed:
                    self._login_alert(device)
                    return
                if s.entered:
                    break
                if s.card_visible and not s.prize and expands < MAX_EXPANDS:
                    # The badge ships collapsed: it shows the entry count but no prize
                    # name and no Enter button. Expanding is a disclosure, not an entry.
                    # It re-collapses on its own after a few seconds, so this retries
                    # rather than giving up after one attempt -- measured live, a single
                    # expand routinely lapsed before the Enter tap could land.
                    expands += 1
                    log.info("expanding giveaway card for %s (attempt %d)", event.show.seller, expands)
                    if device.expand_card():
                        continue
                if s.card_visible and not confirmed:
                    # Confirm gate. The expanded card carries the real prize title and
                    # entry count; the web API exposes neither, so this is the first
                    # point at which value and odds are real. Re-score before tapping.
                    score, parts = score_confirmed(event, s.prize, s.entries, self.cfg.scoring, self.followed)
                    if score < self.cfg.scoring.min_score_confirm:
                        log.info("skipping %s (%s): confirmed score %.1f < %s, parts %s",
                                 s.prize or "?", event.show.seller, score,
                                 self.cfg.scoring.min_score_confirm, parts)
                        self.store.record_entry(event.live_product_id, event.show, device.name, s.entries, "skipped")
                        self.notifier.plain(f"Skipped: {s.prize or '(unknown prize)'} — {event.show.seller} "
                                            f"(score {score:.1f} < {self.cfg.scoring.min_score_confirm:g})")
                        device.go_home()
                        self.idle_until = time.time() + self.cfg.pacing.min_idle_between_shows_s
                        return
                    confirmed = True
                if s.card_visible and s.follow_required:
                    if device.follow_seller():
                        self.followed.add(event.show.seller)
                        self.store.record_follow(event.show.seller)
                        self.notifier.plain(f"Followed: {event.show.seller}")
                    s = device.giveaway_state()
                if s.card_visible and device.enter_giveaway():
                    combined = s.follow_on_enter      # that one tap also followed the seller
                    time.sleep(1.5)
                    s = device.giveaway_state()
                    if s.entered:
                        if combined:
                            self.followed.add(event.show.seller)
                            self.store.record_follow(event.show.seller)
                            self.notifier.plain(f"Followed: {event.show.seller} (required to enter)")
                        break
                if time.time() >= deadline:
                    self.store.record_entry(event.live_product_id, event.show, device.name, s.entries, "missed")
                    self.notifier.plain(f"Missed: {event.prize_text or '(unknown prize)'} — {event.show.seller} (no enter button within timeout)")
                    self.idle_until = time.time() + self.cfg.pacing.min_idle_between_shows_s
                    return
                time.sleep(2)
            if s.entries is not None:
                self.entries_by_id[event.live_product_id] = s.entries
            self.store.record_entry(event.live_product_id, event.show, device.name, s.entries, "entered")
            self.notifier.plain(format_entry(s.prize or event.prize_text, event.show.seller, event.show.game, s.entries, score))
            self.holding = Holding(event, time.time(), device.name)
            self._unknown_screen_strikes = 0
        except Exception as e:
            self._device_error(device, "chase", e)

    def _hold_step(self, device, now: float):
        h = self.holding
        # The watcher replaces (never mutates) events, so ask it what is still running.
        still_active = h.live_product_id in {e.live_product_id for e in self.watcher.active_events()}
        ended = not still_active or (now - h.since) > self.cfg.pacing.hold_timeout_s
        try:
            if device is None or device.name != h.device_name:
                self.store.record_device_event(h.device_name, "lost while holding")
                self.holding = None
                return
            s = device.giveaway_state()
            if s.won:
                self.store.record_win(h.live_product_id, h.event.show, h.event.prize_text or s.prize)
                self.notifier.alert(f"WON: {h.event.prize_text or s.prize or '(unknown prize)'} from {h.event.show.seller}! {h.event.show.url}")
                ended = True
            if ended:
                self.store.end_giveaway(h.live_product_id, now)
                self.holding = None
                self.idle_until = now + self.cfg.pacing.min_idle_between_shows_s
        except Exception as e:
            self._device_error(device, "hold", e)
            self.holding = None

    # ---- errors / alerts ----
    def _login_alert(self, device):
        self.paused = True
        path = self._shot(device, "login")
        self.notifier.alert(f"Whatnot on the {device.name} wants a log in. I've paused; send !resume when it's done.", path)
        self.store.record_error("device", "login needed", path)

    def _device_error(self, device, place: str, e: Exception):
        log.exception("device error in %s", place)
        self._unknown_screen_strikes += 1
        path = self._shot(device, place)
        self.store.record_error(place, str(e), path)
        self.idle_until = time.time() + 30
        if self._unknown_screen_strikes >= 3:
            self.notifier.alert(f"{device.name if device else 'device'} keeps failing in {place}: {e}", path)
            self._unknown_screen_strikes = 0

    def _shot(self, device, label: str) -> str | None:
        try:
            p = str(Path(self.cfg.paths.screens_dir) / f"{int(time.time())}_{label}.png")
            return device.screenshot(p)
        except Exception:
            return None

    # ---- commands ----
    def handle_command(self, cmd: str, device_name: str | None, watching: int):
        if cmd == "pause":
            self.paused = True
            self.notifier.plain("Paused.")
        elif cmd == "resume":
            self.paused = False
            self.notifier.plain("Resumed.")
        elif cmd == "status":
            self.notifier.plain(format_status(self.store.status_summary(24), device_name, self.paused, watching))

    # ---- async wiring ----
    async def run(self, discovery, manager, client):
        watcher_task = asyncio.create_task(self.watcher.run())
        last_discovery = 0.0
        last_cmd = 0.0
        no_device_since: float | None = None
        alerted_no_device = False
        try:
            while True:
                now = time.time()
                if now - last_discovery >= self.cfg.whatnot.discovery_interval_s:
                    try:
                        shows = await asyncio.to_thread(discovery.poll)
                        for s in shows:
                            self.store.upsert_show(s)
                        self.store.mark_ended_except({s.id for s in shows})
                        self.watcher.set_shows(shows, self.followed)
                    except Exception as e:
                        log.warning("discovery failed: %s", e)
                    last_discovery = now
                device = await asyncio.to_thread(manager.tick)
                for dev_name, event in manager.drain_events():
                    self.store.record_device_event(dev_name, event)
                    self.notifier.plain(f"Device: {dev_name} {event}")
                if device is None:
                    no_device_since = no_device_since or now
                    if not alerted_no_device and now - no_device_since > 300:
                        self.notifier.alert("No device available (phone undocked and emulator not up). Idle until one appears.")
                        alerted_no_device = True
                else:
                    no_device_since, alerted_no_device = None, False
                if now - last_cmd >= self.cfg.discord.command_poll_s:
                    for cmd in await asyncio.to_thread(self.notifier.poll_commands):
                        self.handle_command(cmd, device.name if device else None, len(self.watcher.states))
                    last_cmd = now
                await asyncio.to_thread(self.step)
                await asyncio.sleep(3)
        finally:
            watcher_task.cancel()
