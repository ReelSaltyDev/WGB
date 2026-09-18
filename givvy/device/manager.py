"""Chooses the active device: phone when docked, emulator after a grace period otherwise."""
from __future__ import annotations

import logging
import time

from ..config import DeviceCfg

log = logging.getLogger(__name__)


class DeviceManager:
    def __init__(self, phone, emulator, dcfg: DeviceCfg):
        self.phone = phone
        self.emulator = emulator
        self.cfg = dcfg
        self.events: list[tuple[str, str]] = []   # (device, event) since last drain
        self._phone_missing_since: float | None = None
        self._phone_back_since: float | None = None
        self._phone_was_available: bool | None = None
        self.current = None

    def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        phone_ok = bool(self.phone and self.phone.is_available())
        if self._phone_was_available is not None and phone_ok != self._phone_was_available:
            self.events.append(("phone", "docked" if phone_ok else "undocked"))
        self._phone_was_available = phone_ok
        if phone_ok:
            self._phone_missing_since = None
            self._phone_back_since = self._phone_back_since or now
            if self.emulator and self.emulator.is_available() and now - self._phone_back_since >= self.cfg.emulator_stop_after_phone_back_s:
                self.emulator.stop()
                self.events.append(("emulator", "stopped"))
            self.current = self.phone
            return self.phone
        self._phone_back_since = None
        self._phone_missing_since = self._phone_missing_since or now
        if self.emulator is None:
            self.current = None
            return None
        if self.emulator.is_available():
            self.current = self.emulator
            return self.emulator
        if now - self._phone_missing_since >= self.cfg.emulator_boot_after_phone_missing_s:
            if self.emulator.boot():
                self.events.append(("emulator", "booted"))
                self.current = self.emulator
                return self.emulator
            self.events.append(("emulator", "boot_failed"))
            # retry in 15 min: _phone_missing_since is compared against
            # emulator_boot_after_phone_missing_s again on the next tick, so
            # back it off by that amount too or the real wait becomes
            # 900 + emulator_boot_after_phone_missing_s instead of 900.
            self._phone_missing_since = now + 900 - self.cfg.emulator_boot_after_phone_missing_s
        self.current = None
        return None

    def drain_events(self) -> list[tuple[str, str]]:
        ev, self.events = self.events, []
        return ev
