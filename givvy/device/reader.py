"""A screen reader that stays running inside the emulator (or phone), instead of starting a fresh one per look.

The old way, `uiautomator dump`, starts a Java program inside Android for every single read: measured 3.2 s a read
on a live stream (median, 2026-09-23, emulator 2). This uses uiautomator2 (openatx, MIT, from PyPI): it pushes its
bundled u2.jar to /data/local/tmp, runs it with app_process (no app is installed) and asks it over adb for the
screen. Measured on the same stream: 1.3-1.6 s a read. The rest is Android walking the screen's contents while live
video and chat keep it busy, which no reader can avoid.

Android allows ONE screen-reading session per device, so while this runs the old `uiautomator dump` fails. That is
why a failure here stops the helper before the caller falls back to the old way.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class FastReader:
    RETRY_AFTER_S = 60.0       # after a failure, use the old way for this long before trying again
    TIMEOUT_S = 12.0           # a read (or starting the helper) that takes longer than this counts as a failure

    def __init__(self, serial: str, adb_path: str | None = None, connect=None):
        self.serial = serial
        if adb_path and Path(adb_path).exists():
            # uiautomator2 talks to the adb SERVER; make sure anything it starts is the SDK's own adb, so two
            # different adb versions never fight over the server (that disconnects every device).
            os.environ.setdefault("ADBUTILS_ADB_PATH", adb_path)
        self._connect_fn = connect
        self._d = None
        self._broken_until = 0.0
        self._lock = threading.Lock()
        self.reads = 0
        self.failures = 0

    def _connect(self):
        if self._connect_fn is not None:
            return self._connect_fn(self.serial)
        import uiautomator2 as u2
        return u2.connect(self.serial)

    def _within(self, fn):
        """Run fn with a hard time limit. Seen in the packaged build: a helper call that never returned would
        have frozen that account's loop for good. A call that overruns is abandoned (its thread is a daemon)."""
        box: dict = {}

        def run():
            try:
                box["value"] = fn()
            except BaseException as e:           # noqa: BLE001 - handed to the caller
                box["error"] = e
        t = threading.Thread(target=run, daemon=True, name=f"reader-{self.serial}")
        t.start()
        t.join(self.TIMEOUT_S)
        if t.is_alive():
            raise TimeoutError(f"no answer from the screen reader helper in {self.TIMEOUT_S:.0f}s")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def dump(self) -> str | None:
        """The screen as uiautomator XML, or None: then the caller reads it the old way."""
        with self._lock:
            if time.time() < self._broken_until:
                return None
            for attempt in (1, 2):                     # a rebooted emulator needs the helper started again
                try:
                    if self._d is None:
                        self._d = self._within(self._connect)
                        log.info("%s: screen reader helper running", self.serial)
                    d = self._d
                    xml = self._within(lambda: d.dump_hierarchy(compressed=False))
                    self.reads += 1
                    return xml
                except Exception as e:
                    self._d = None
                    if attempt == 2:
                        self.failures += 1
                        self._broken_until = time.time() + self.RETRY_AFTER_S
                        log.warning("%s: screen reader helper failed (%s); using the old way for %ds",
                                    self.serial, e, int(self.RETRY_AFTER_S))
                        self._stop_quietly()
            return None

    def _stop_quietly(self) -> None:
        """Free the device's one screen-reading session for the old way."""
        try:
            d = self._d or self._within(self._connect)
            self._within(d.stop_uiautomator)
        except Exception:
            pass
        self._d = None

    def stop(self) -> None:
        with self._lock:
            self._stop_quietly()
