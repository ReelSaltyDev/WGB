"""AndroidDevice: drives the Whatnot app through plain adb.

No agent is installed on the phone. Verified on a Galaxy S24 Ultra (Android 16):
`uiautomator dump` for the hierarchy, `input tap` for taps, `exec-out screencap`
for screenshots, `dumpsys window`/`dumpsys power` for foreground app and screen
state. The same class drives the emulator.
"""
from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path

from ..config import DeviceCfg, PacingCfg
from . import GiveawayScreen
from . import adb as adb_mod
from . import selectors as S

log = logging.getLogger(__name__)

_FOCUS = re.compile(r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z0-9_.]+)/")
_AWAKE = re.compile(r"mWakefulness=(\w+)")
DUMP_PATH = "/sdcard/_givvy_ui.xml"


class AndroidDevice:
    def __init__(self, name: str, serial: str, dcfg: DeviceCfg, pacing: PacingCfg,
                 dry_run: bool = False, adb=adb_mod):
        self.name = name
        self.serial = serial
        self.dcfg = dcfg
        self.pacing = pacing
        self.dry_run = dry_run
        self.adb = adb
        self._last_url: str | None = None
        self._read_at = 0.0             # time the last screen read started
        self.reader = None              # device.reader.FastReader: the persistent helper, when enabled
        self.recorder = None            # replay.Recorder: every read and tap, when a session is being recorded
        self.keep_screens = False       # write the latest screen to logs/screen-<name>.xml (real devices only)

    # ---- plumbing ----
    def _shell(self, *args: str, timeout: int = 30) -> str:
        if self.recorder is not None and args and args[0] in ("input", "am", "svc", "monkey"):
            try:
                self.recorder.command(time.time(), list(args))       # taps, keys, stream links: for replay
            except Exception:
                pass
        return self.adb.shell(self.dcfg.android_sdk, self.serial, *args, timeout=timeout)

    def _nodes(self) -> list[dict]:
        self._read_at = time.time()
        xml = self.reader.dump() if self.reader is not None else None
        if xml is None:                 # no helper, or it failed: the old way, a fresh uiautomator per read
            self._shell("uiautomator", "dump", DUMP_PATH, timeout=60)
            xml = self.adb.exec_out(self.dcfg.android_sdk, self.serial, "cat", DUMP_PATH).decode("utf-8", errors="replace")
        self._keep(xml)
        return S.parse_nodes(xml)

    def _keep(self, xml: str) -> None:
        """The latest screen, beside the logs, for diagnosis (the helper no longer leaves it on the device);
        and every read to the recorder while a session is being recorded."""
        if self.keep_screens:           # not for devices made in tests: they would write into the real logs/
            try:
                (Path.cwd() / "logs").mkdir(exist_ok=True)
                (Path.cwd() / "logs" / f"screen-{self.name}.xml").write_text(xml, encoding="utf-8")
            except Exception:
                pass
        if self.recorder is not None:
            try:
                self.recorder.read(self._read_at, xml)
            except Exception:
                log.exception("recorder")

    def snapshot(self) -> tuple[list[dict], str]:
        """One screen read that also answers 'what app is in front'.

        Every node in a uiautomator dump carries its package, so the two
        dumpsys calls the busy check used to make per tick (~0.3s and two
        subprocesses) are redundant while camped. A dump taken mid-animation
        can come back empty; that is transient, so it is retried once.
        """
        ns = self._nodes()
        if not ns:
            time.sleep(0.3)
            ns = self._nodes()
        pkg = S.foreground_package(ns)
        return ns, pkg

    def _pause(self, critical: bool = False):
        """Human-ish gap before a tap. Inside an open giveaway card the gap is
        much shorter: the card lapses in seconds and pacing is enforced by the
        hourly cap and the idle gaps between streams, not here."""
        if critical:
            lo, hi = self.pacing.enter_tap_delay_min_s, self.pacing.enter_tap_delay_max_s
        else:
            lo, hi = self.pacing.tap_delay_min_s, self.pacing.tap_delay_max_s
        time.sleep(random.uniform(lo, hi))

    def _tap(self, xy: tuple[int, int], what: str, critical: bool = False) -> None:
        self._pause(critical)
        if self.dry_run:
            log.info("[dry-run] would tap %s at %s", what, xy)
            return
        log.info("tap %s at %s (coords from a read %.1fs old)", what, xy, time.time() - self._read_at)
        self._shell("input", "tap", str(xy[0]), str(xy[1]))

    # ---- Device protocol ----
    def is_available(self) -> bool:
        try:
            return self.adb.list_devices(self.dcfg.android_sdk).get(self.serial) == "device"
        except Exception as e:
            log.warning("adb devices failed: %s", e)
            return False

    def app_installed(self) -> bool:
        """Is Whatnot on this device? A freshly created emulator is up long before
        anyone has installed the app and signed in; without this the camper claimed
        a stream and fired deep links into an empty launcher. A yes is remembered;
        a no is re-checked at most every 30s (it also covers 'still booting')."""
        if getattr(self, "_app_ok", False):
            return True
        now = time.time()
        if now - getattr(self, "_app_checked", 0.0) < 30:
            return False
        self._app_checked = now
        try:
            out = self._shell("pm", "list", "packages", self.dcfg.whatnot_package)
        except Exception:
            return False
        self._app_ok = f"package:{self.dcfg.whatnot_package}" in (out or "")
        return self._app_ok

    def is_user_busy(self, foreground: str | None = None) -> bool:
        """True when the owner is using the phone for something else.

        Pass the package from a snapshot() to avoid two dumpsys calls."""
        if foreground is not None:
            return foreground not in ({self.dcfg.whatnot_package} | S.LAUNCHERS)
        try:
            m = _AWAKE.search(self._shell("dumpsys", "power"))
            if m and m.group(1) != "Awake":
                return False                      # screen off: not in use
            f = _FOCUS.search(self._shell("dumpsys", "window"))
        except Exception as e:
            log.warning("foreground check failed: %s", e)
            return True                           # unknown: leave the phone alone
        if f is None:
            return False
        return f.group(1) not in ({self.dcfg.whatnot_package} | S.LAUNCHERS)

    def _clear_system_ui(self) -> None:
        """Dismiss anything the system has put over the app.

        Observed on the real phone: the notification shade ends up pulled down
        (mCurrentFocus=NotificationShade) and every subsequent deep link is
        swallowed, so the bot chases shows it can never reach. `statusbar
        collapse` is the documented way to close it; BACK is the fallback for
        other overlays.
        """
        try:
            focus_line = self._shell("dumpsys", "window")
            f = _FOCUS.search(focus_line)
            pkg = f.group(1) if f else ""
        except Exception:
            return
        if "NotificationShade" in focus_line or pkg.endswith("systemui"):
            log.info("system UI is covering the app; collapsing it")
            self._shell("cmd", "statusbar", "collapse")
            time.sleep(0.6)
            try:
                if "NotificationShade" in self._shell("dumpsys", "window"):
                    self._shell("input", "keyevent", "KEYCODE_BACK")
                    time.sleep(0.4)
            except Exception:
                pass

    def _fire_intent(self, url: str) -> None:
        self._clear_system_ui()
        self._shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url, self.dcfg.whatnot_package)

    def _in_stream(self) -> bool:
        """Are we actually inside a livestream, or just looking at the app?"""
        return self._arrival() == "ok"

    def _arrival(self) -> str:
        """'ok' in a stream, 'tariff' stopped at the tariff interstitial, else 'missing'."""
        try:
            ns = self._nodes()
        except Exception as e:
            log.warning("in-stream check failed: %s", e)
            return "missing"
        if S.find(ns, S.IN_STREAM) is not None:
            return "ok"
        if S.tariff_notice(ns):
            return "tariff"
        return "missing"

    def open_show(self, url: str) -> None:
        """Open a show and CONFIRM arrival.

        The deep link is not reliable on its own: measured on the real phone, an
        already-running Whatnot can swallow the intent and sit on its home screen
        (e.g. behind the account-verification banner), after which the chase just
        polls an empty screen until it times out. So poll for a positive
        in-stream marker and, if it never appears, force-stop and try once more.
        """
        self._last_url = url
        self._fire_intent(url)
        got = self._await_stream()
        if got != "missing":
            return got
        log.warning("deep link did not land in a stream; force-stopping and retrying")
        self._shell("am", "force-stop", self.dcfg.whatnot_package)
        time.sleep(1.0)
        self._fire_intent(url)
        got = self._await_stream()
        if got == "missing":
            log.warning("still not in a stream after retry: %s", url)
        return got

    def _await_stream(self) -> str:
        """Seen live: a seller shipping from abroad shows 'ATTENTION - additional
        tariffs will apply' INSTEAD of the stream. Waiting it out and force-stopping
        just produced the same popup again, so report it as its own outcome."""
        deadline = time.time() + self.pacing.open_settle_max_s
        time.sleep(self.pacing.open_settle_min_s)
        while time.time() < deadline:
            got = self._arrival()
            if got != "missing":
                return got
            time.sleep(1.0)
        return "missing"

    def join_past_notice(self) -> bool:
        """Tap 'Join Show' on the tariff interstitial. Only used for a stream the
        user pinned themselves. Never taps 'Don't show this again': that popup is
        how foreign sellers are recognised."""
        join = S.find(self._nodes(), S.JOIN_SHOW)
        if join is None:
            return False
        self._tap(S.center(join), "Join Show")
        return True

    def current_show(self) -> str | None:
        if not self._last_url:
            return None
        try:
            f = _FOCUS.search(self._shell("dumpsys", "window"))
        except Exception:
            return None
        if f is None or f.group(1) != self.dcfg.whatnot_package:
            return None
        return self._last_url.rstrip("/").rsplit("/", 1)[-1]

    def giveaway_state(self) -> GiveawayScreen:
        ns = self._nodes()
        enter = S.find(ns, S.ENTER_BUTTON)
        entered = S.is_entered(ns)
        return GiveawayScreen(
            card_visible=S.card_visible(ns) or entered or enter is not None,
            entered=entered,
            entries=S.entries_count(ns),
            follow_required=S.find(ns, S.FOLLOW_TO_ENTER) is not None,
            prize=S.prize_title(ns),
            won=S.is_won(ns, getattr(self, "usernames", frozenset())),
            login_needed=S.find(ns, S.LOGIN) is not None and enter is None and not entered,
            follow_on_enter=enter is not None and S.FOLLOW_AND_ENTER.search(S.text_of(enter)) is not None,
            overlay=S.overlay_open(ns),
            tariff_notice=S.tariff_notice(ns),
            region_only=S.region_only(ns),
            # Any ONE stream marker is enough; saying 'not a stream' needs all of them missing, and an
            # empty read (the dump file caught mid-write) says nothing either way.
            in_stream=(not ns or S.find(ns, S.IN_STREAM) is not None or entered or enter is not None
                       or S.card_visible(ns) or S.overlay_open(ns) or S.tariff_notice(ns)),
        )

    def expand_card(self) -> bool:
        """Tap the collapsed giveaway badge so the prize and Enter button appear.
        This is a disclosure, not an entry, so it runs even under --dry-run."""
        xy = S.badge_tap_point(self._nodes())
        if xy is None:
            return False
        self._pause()
        self._shell("input", "tap", str(xy[0]), str(xy[1]))
        time.sleep(1.0)
        return True

    def read_and_locate(self) -> tuple[GiveawayScreen, tuple[int, int] | None, bool]:
        """One dump -> (screen, where the Enter button is, was-it-combined).

        The expanded giveaway card re-collapses after a few seconds. Reading the
        screen, deciding, and then re-reading to find the button took long enough
        that the card was usually gone by the tap. This returns everything needed
        from a single dump so the caller can decide and tap immediately.
        """
        return self._screen_from(self._nodes())

    def _screen_from(self, ns: list[dict]) -> tuple[GiveawayScreen, tuple[int, int] | None, bool]:
        enter = S.find(ns, S.ENTER_BUTTON)
        entered = S.is_entered(ns)
        screen = GiveawayScreen(
            card_visible=S.card_visible(ns) or entered or enter is not None,
            entered=entered,
            entries=S.entries_count(ns),
            follow_required=S.find(ns, S.FOLLOW_TO_ENTER) is not None,
            prize=S.prize_title(ns),
            won=S.is_won(ns, getattr(self, "usernames", frozenset())),
            login_needed=S.find(ns, S.LOGIN) is not None and enter is None and not entered,
            follow_on_enter=enter is not None and S.FOLLOW_AND_ENTER.search(S.text_of(enter)) is not None,
            overlay=S.overlay_open(ns),
            tariff_notice=S.tariff_notice(ns),
            region_only=S.region_only(ns),
            # Any ONE stream marker is enough; saying 'not a stream' needs all of them missing, and an
            # empty read (the dump file caught mid-write) says nothing either way.
            in_stream=(not ns or S.find(ns, S.IN_STREAM) is not None or entered or enter is not None
                       or S.card_visible(ns) or S.overlay_open(ns) or S.tariff_notice(ns)),
        )
        xy = S.center(enter) if enter is not None else None
        return screen, xy, screen.follow_on_enter

    def tap_at(self, xy: tuple[int, int], what: str = "Enter") -> bool:
        """Tap a point found in an earlier dump, with no fresh dump in between."""
        self._tap(xy, what, critical=True)
        return True

    def expand_then_read(self, ns: list[dict] | None = None
                         ) -> tuple[GiveawayScreen, tuple[int, int] | None, bool]:
        """Open the card if it is closed, then read it.

        Takes the nodes the caller already has, so a collapsed card costs one
        tap and one read, not two reads. Only taps when the card is actually
        collapsed: tapping an open card closes it. After the tap it polls in
        short steps rather than sleeping a fixed 0.9s, and returns as soon as
        the open layout appears.
        """
        if ns is None:
            ns = self._nodes()
        if S.is_expanded(ns):
            return self._screen_from(ns)
        xy = S.badge_tap_point(ns)
        if xy is None:
            return self._screen_from(ns)
        if self.dry_run:
            log.info("[dry-run] would tap giveaway badge at %s", xy)
        else:
            log.info("%s: tap badge at %s (coords from a read %.1fs old)", self.name, xy, time.time() - self._read_at)
            self._shell("input", "tap", str(xy[0]), str(xy[1]))
        # A read costs ~2.6s on a live stream (uiautomator waits for an idle UI
        # that a scrolling chat never provides), so this is one read after a
        # settle long enough for the card to open, and a single retry.
        for _ in range(2):
            time.sleep(self.pacing.expand_settle_s)
            ns = self._nodes()
            if S.is_expanded(ns) or not S.card_visible(ns):
                break
        return self._screen_from(ns)

    FAST_SETTLE_S = 0.9        # 0.6 was too tight: seen live, the Enter tap landed before the card had opened

    def fast_enter(self, ns: list[dict]) -> bool:
        """Tap the collapsed badge and then Enter where it WILL be, with no read in between.

        A read costs 2.6-4 s, and in break streams a 'Selecting Spot' banner takes the card's place for 4-5 s
        after every auction, so the usual badge-read-Enter needed more clear seconds than there were. The
        predicted spot is inside the card we have just opened, near the top of the screen: nowhere near a
        bid, buy or payment control, which all sit in the bottom third. False = nothing was tapped."""
        if S.is_expanded(ns):
            return False
        badge, enter = S.badge_tap_point(ns), S.enter_point_from_badge(ns)
        if badge is None or enter is None:
            return False
        if self.dry_run:
            log.info("[dry-run] %s would fast-enter via %s then %s", self.name, badge, enter)
            return False
        log.info("%s: fast enter: badge %s then Enter %s (from a read %.1fs old)", self.name, badge, enter,
                 time.time() - self._read_at)
        self._shell("input", "tap", str(badge[0]), str(badge[1]))
        time.sleep(self.FAST_SETTLE_S)
        self._shell("input", "tap", str(enter[0]), str(enter[1]))
        return True

    def entered_by_tick(self, ns: list[dict]) -> bool | None:
        """Read the collapsed badge's icon from a screenshot: True = the tick (we are in), False = the gift
        (we are not), None = cannot tell. Costs one screenshot, so it is only asked when proof is needed."""
        if S.is_expanded(ns):
            return None
        box = S.badge_icon_box(ns)
        if box is None:
            return None
        try:
            import io
            from PIL import Image
            from .tick import ENTERED, NOT_ENTERED, crop_box, icon_state
            png = self.adb.exec_out(self.dcfg.android_sdk, self.serial, "screencap", "-p")
            if self.recorder is not None:
                self.recorder.screenshot(time.time(), png)
            state = icon_state(Image.open(io.BytesIO(png)).crop(crop_box(box)))
        except Exception as e:
            log.warning("%s: could not read the badge icon: %s", self.name, e)
            return None
        log.info("%s: badge icon reads %s", self.name, state or "neither (layout changed under the screenshot)")
        return True if state == ENTERED else (False if state == NOT_ENTERED else None)

    def verify_entered(self) -> GiveawayScreen:
        """After the Enter tap: short polls until the app confirms, up to ~1.5s."""
        s = None
        for _ in range(2):                      # one read, one retry: reads are ~2.6s
            time.sleep(self.pacing.verify_settle_s)
            s = self.giveaway_state()
            if s.entered or not s.card_visible:
                break
        return s

    def follow_seller(self) -> bool:
        n = S.find(self._nodes(), S.FOLLOW_BUTTON)
        if n is None:
            return False
        xy = S.center(n)
        if xy is None:
            return False
        self._tap(xy, "Follow")
        return True

    def enter_giveaway(self) -> bool:
        n = S.find(self._nodes(), S.ENTER_BUTTON)
        if n is None:
            return False
        xy = S.center(n)
        if xy is None:
            return False
        self._tap(xy, "Enter")
        return True

    def screenshot(self, path: str) -> str:
        data = self.adb.exec_out(self.dcfg.android_sdk, self.serial, "screencap", "-p")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def dismiss_overlay(self) -> bool:
        """Close a sheet or modal covering the stream. BACK closes Whatnot's
        bottom sheets and dialogs without leaving the stream."""
        log.info("overlay covering the stream; sending BACK to dismiss it")
        self._shell("input", "keyevent", "KEYCODE_BACK")
        time.sleep(0.8)
        return True

    def go_home(self) -> None:
        try:
            self._shell("input", "keyevent", "KEYCODE_HOME")
        except Exception as e:
            log.warning("home failed: %s", e)
