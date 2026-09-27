"""Alerts that reach you without Discord.

2026-09-24 04:30-06:10: both accounts' emulators were down for an hour and a half and nobody heard. The Discord
bot_token and channel_id in John's config.toml are blank, which disables the Notifier: every alert only became a
'discord: ...' line in givvy.log. Now every alert is a line in alerts.log, a red banner in the window and a Windows
notification, whatever else is set up. Discord gets it too when it is set up, in a channel of its own when
[discord] alerts_channel_id is set: the routine posts (camps, entries, skips) run about 700 a day and would bury it.

One alert per incident. Its key ('boot:<device>', 'breaker:<account>', 'login:<account>', 'out:<account>') stays
active, and nothing more is sent for it, until it is resolved: no repeats."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import DiscordCfg
from .notify import DiscordHttp

log = logging.getLogger(__name__)

TITLE = "Whatnot Givvy needs you"


@dataclass
class Alert:
    key: str                  # the incident
    text: str
    account: str
    at: float
    shot: str | None = None   # a screenshot, posted to Discord with it
    seen: bool = False        # dismissed from the banner; still active until resolved


class Alerts:
    """fire() and resolve() write alerts.log at once, on the caller's thread. Windows and Discord are sent by one worker
    thread, so a camper or the engine never waits on them: DiscordHttp retries for about 31 s when Discord is down."""

    ICON_WAIT_S = 5.0         # for the tray icon to appear before its first notification

    def __init__(self, path, discord: DiscordCfg, windows: bool = True, http=None, notify=None):
        self.path = Path(path)
        self.discord = discord
        self.windows = windows
        self.on_show = None                   # callable(): bring the window forward (set by the window)
        self._http = http                     # tests: a stand-in for DiscordHttp
        self._notify = notify                 # tests: a stand-in for the Windows notification, notify(title, text)
        self._active: dict[str, Alert] = {}   # oldest first
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._icon = None
        self._warned = False

    # ---- incidents ----
    def fire(self, key: str, text: str, account: str = "", shot: str | None = None) -> bool:
        """A new incident. False, and nothing written or sent, while `key` is still active."""
        with self._lock:
            if key in self._active:
                return False
            self._active[key] = Alert(key, text, account, time.time(), shot)
            self._write("ALERT", account, text)
        log.error("ALERT: %s%s", f"[{account}] " if account else "", text)
        self._send(TITLE + (f" ({account})" if account else ""), text, shot, mention=True)
        return True

    def restore(self, key: str, text: str, account: str = "", at: float | None = None) -> bool:
        """An incident that began before an app restart and is still going on (a safety pause, Camper.load_breaker):
        active again, in the banner and holding back the out-of-stream alert, with nothing written or sent, since its
        ALERT line and notification went out when it began. False while `key` is already active."""
        with self._lock:
            if key in self._active:
                return False
            self._active[key] = Alert(key, text, account, time.time() if at is None else at)
        return True

    def resolve(self, key: str, text: str = "") -> bool:
        """The incident is over. Windows and Discord hear of it only when `text` says what happened: a Resume you
        pressed, or a Stop, needs no notification. False when `key` was not active."""
        with self._lock:
            a = self._active.pop(key, None)
            if a is None:
                return False
            self._write("RESOLVED", a.account, text or f"{key} cleared")
        log.info("RESOLVED: %s%s", f"[{a.account}] " if a.account else "", text or key)
        if text:
            self._send("Whatnot Givvy" + (f" ({a.account})" if a.account else "") + ": resolved", text)
        return True

    def active(self, account: str | None = None) -> list[Alert]:
        """Newest first; only `account`'s when given."""
        with self._lock:
            return [a for a in reversed(self._active.values()) if account is None or a.account == account]

    def banner(self) -> str:
        """The window's red banner: the active alerts not dismissed, newest first, at most 3 lines."""
        shown = [a for a in self.active() if not a.seen][:3]
        return "\n".join(f"{time.strftime('%H:%M', time.localtime(a.at))}  {a.text}" for a in shown)

    def dismiss(self) -> None:
        """Hides the banner. The alerts stay active, so nothing fires again for them until they are resolved."""
        with self._lock:
            for a in self._active.values():
                a.seen = True

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Tests: until everything queued has been delivered, or has failed."""
        with self._q.all_tasks_done:
            return self._q.all_tasks_done.wait_for(lambda: not self._q.unfinished_tasks, timeout)

    def close(self) -> None:
        """The app is closing: take the tray icon away."""
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    # ---- delivery ----
    def _write(self, kind: str, account: str, text: str) -> None:
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {kind}  " + (f"[{account}] " if account else "")
                + " ".join(text.split()))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            log.warning("could not write %s: %s", self.path, e)

    def _send(self, title: str, text: str, shot: str | None = None, mention: bool = False) -> None:
        self._q.put((title, text, shot, mention))
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="alerts", daemon=True)
                self._worker.start()

    def _work(self) -> None:
        while True:
            title, text, shot, mention = self._q.get()
            try:
                if self.windows:
                    self._to_windows(title, text)
                self._to_discord(text, shot, mention)
            except Exception:
                log.exception("alert delivery failed")
            finally:
                self._q.task_done()

    def _to_windows(self, title: str, text: str) -> None:
        # NOTIFYICONDATA holds 256 characters of text and 64 of title; a longer one is refused outright.
        title, text = title[:60], text[:250]
        try:
            if self._notify is not None:
                self._notify(title, text)
                return
            if self._icon is None:
                self._icon = self._make_icon()     # stays for the session
            self._icon.notify(text, title)
        except Exception as e:
            if not self._warned:
                self._warned = True
                log.warning("no Windows notification (%s); alerts still go to %s and the window's banner",
                            e, self.path.name)

    def _make_icon(self):
        """A red tray icon: Windows shows a notification only from one. Clicking it brings the window forward."""
        import pystray
        from .tray import _icon
        icon = pystray.Icon("givvy-alerts", _icon("red"), TITLE,
                            pystray.Menu(pystray.MenuItem("Show Whatnot Givvy", self._show, default=True)))
        threading.Thread(target=icon.run, name="alerts-icon", daemon=True).start()
        end = time.time() + self.ICON_WAIT_S
        while not icon.visible and time.time() < end:
            time.sleep(0.05)
        return icon

    def _show(self) -> None:
        if self.on_show is not None:
            try:
                self.on_show()
            except Exception:
                log.exception("could not bring the window forward")

    def _to_discord(self, text: str, shot: str | None, mention: bool) -> None:
        d = self.discord
        channel = d.alerts_channel_id or d.channel_id
        if not (d.bot_token and channel):
            return
        try:
            http = self._http or DiscordHttp(d.bot_token, channel)
            content = (f"{d.mention} {text}" if mention else text).strip()[:1900]
            http.post_message(content, shot if shot and Path(shot).is_file() else None)
        except Exception as e:
            log.warning("discord alert failed: %s", e)
