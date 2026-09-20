"""Start, restart and cleanly power off one Android emulator (AVD)."""
from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

from ..config import DeviceCfg, PacingCfg
from . import adb as adb_mod
from .android import AndroidDevice

log = logging.getLogger(__name__)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# What the emulator prints when it cannot start -> (which repair to offer, plain words).
# It used to be launched with its output thrown away, so a failed start looked like
# 'booting...' forever. The first entry is what a PC that has never run an emulator hits.
_FAILURES = (
    (re.compile(r"requires hardware acceleration|WHPX|HAXM|AEHD|hypervisor", re.I), "hypervisor",
     "The emulator cannot use hardware acceleration: Windows Hypervisor Platform is off, or virtualisation is "
     "disabled in the BIOS."),
    (re.compile(r"Unknown AVD name|no file .*\.ini", re.I), "setup",
     "This emulator has not been created yet."),
    (re.compile(r"VCRUNTIME|MSVCP140|0xc000007b", re.I), "vcredist",
     "Microsoft's Visual C++ runtime is missing, so the emulator program cannot start."),
    (re.compile(r"not enough (disk )?space", re.I), "disk",
     "Not enough free disk space to start the emulator."),
    (re.compile(r"multiple emulators with the same AVD|is already running", re.I), "already-running",
     "This emulator is already running in another window."),
)


def explain_emulator_failure(output: str) -> tuple[str, str]:
    """(reason in plain words, repair key) from the emulator's own output."""
    for pattern, fix, reason in _FAILURES:
        if pattern.search(output or ""):
            return reason, fix
    errors = [l.split("|", 1)[-1].strip() for l in (output or "").splitlines()
              if l.strip().upper().startswith(("ERROR", "FATAL"))]
    return (errors[-1] if errors else "The emulator closed without saying why."), ""


class EmulatorDevice(AndroidDevice):
    def __init__(self, dcfg: DeviceCfg, pacing: PacingCfg, dry_run: bool = False,
                 name: str = "emulator", avd: str | None = None, port: int = 5554, headless: bool = False,
                 adb=adb_mod):
        # The console port fixes the adb serial, which is how several emulators
        # (one per account) are told apart.
        super().__init__(name, f"emulator-{port}", dcfg, pacing, dry_run, adb=adb)
        self.avd = avd or dcfg.avd_name
        self.port = port
        self.headless = headless
        self.poll_s = 2.0
        self.cpu_affinity = ""          # e.g. "0-3"; Windows forgets it at every launch, so boot() re-applies it
        self.priority = "normal"
        self.last_error = ""             # why the last boot failed, in plain words (shown in the window)
        self.last_fix = ""               # which repair to offer: hypervisor | vcredist | setup | disk | ...
        self.log_path: Path | None = None
        self._proc: subprocess.Popen | None = None
        self.settle_s = 6.0              # after switching mobile data off, before checking the Wi-Fi held
        self._net_checked_at = 0.0       # keep_online(): last look
        self._wifi_toggled_at = 0.0
        self._keep_mobile_until = 0.0    # Wi-Fi did not hold without it: leave the mobile link alone for a while

    # ---- state ----
    def is_available(self) -> bool:
        """Only OUR emulator counts. It used to accept any emulator-*, which with
        two accounts would have had both campers driving the same one."""
        try:
            up = self.adb.list_devices(self.dcfg.android_sdk).get(self.serial) == "device"
            if up and time.time() - getattr(self, "_net_checked_at", 0.0) >= self.NET_CHECK_S:
                self.keep_online()
            return up
        except Exception as e:
            log.warning("adb devices failed: %s", e)
            return False

    def is_running(self) -> bool:
        """Up in any state, including 'offline' while Android is still booting."""
        try:
            return self.serial in self.adb.list_devices(self.dcfg.android_sdk)
        except Exception:
            return False

    # ---- start ----
    def _spawn(self, args: list[str]) -> None:
        """Keep what the emulator says. It used to go to DEVNULL, which is why a
        failed start gave no reason at all."""
        logs = Path.cwd() / "logs"
        logs.mkdir(exist_ok=True)
        self.log_path = logs / f"emulator-{self.name}.log"
        out = self.log_path.open("w", encoding="utf-8", errors="replace")
        out.write(" ".join(args) + "\n")
        out.flush()
        self._proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)

    def _proc_exited(self) -> bool:
        return self._proc is not None and self._proc.poll() is not None

    def _log_tail(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if self.log_path else ""
        except OSError:
            return ""

    @staticmethod
    def _wifi_has_internet(connectivity_dump: str) -> bool:
        """Android's own verdict. 'Connected' is not enough: measured on one of three identical emulators,
        Wi-Fi was connected with an address but never VALIDATED (no IPv4 route), everything really went
        over the pretend mobile connection, and switching that off cut the emulator off."""
        return any("WIFI" in line and "IS_VALIDATED" in line
                   for line in connectivity_dump.splitlines() if "NetworkAgentInfo{" in line)

    NET_CHECK_S = 60           # how often the connection is looked at
    WIFI_RETRY_S = 300         # a Wi-Fi without internet is switched off and on at most this often
    HOLD_OFF_S = 1800          # after Wi-Fi failed to hold on its own, do not try again for this long

    def keep_online(self, now: float | None = None) -> str:
        """Never offline, and no Android 'data warning' when it can be helped. Looked at every minute.

        An emulator has a pretend mobile connection beside its Wi-Fi and counts traffic against that 'plan'
        (2 GB warning; live video passes it within the hour). With the mobile data off the warnings stop.
        But on some boots the Wi-Fi comes up connected and WITHOUT internet (address, no IPv4 route; seen on
        two of three identical emulators in one afternoon), and 'mobile data off' survives a reboot. Doing
        this once left an emulator offline after its next restart. So it is a standing rule, not a switch:
          Wi-Fi has internet  -> mobile data off (checked afterwards; put back if the Wi-Fi does not hold)
          Wi-Fi has none      -> mobile data ON at once as the fallback, and the Wi-Fi switched off and on
        Returns what it did, for the log and the tests."""
        now = time.time() if now is None else now
        self._net_checked_at = now
        if getattr(self.dcfg, "emulator_mobile_data", False):
            return "left alone"
        try:
            wifi_ok = self._wifi_has_internet(self._shell("dumpsys", "connectivity"))
            mobile_on = self._shell("settings", "get", "global", "mobile_data").strip() != "0"
            if not wifi_ok:
                did = []
                if not mobile_on:
                    self._shell("svc", "data", "enable")
                    log.warning("%s: Wi-Fi has no internet; mobile connection switched back on as the fallback", self.name)
                    did.append("mobile on")
                if now - self._wifi_toggled_at >= self.WIFI_RETRY_S:
                    self._wifi_toggled_at = now
                    self._shell("svc", "wifi", "disable")
                    time.sleep(min(self.settle_s, 4.0))
                    self._shell("svc", "wifi", "enable")
                    log.info("%s: Wi-Fi switched off and on to get its internet back", self.name)
                    did.append("wifi toggled")
                return ", ".join(did) or "waiting for wifi"
            if mobile_on and now >= self._keep_mobile_until:
                self._shell("svc", "data", "disable")
                time.sleep(self.settle_s)
                if not self._wifi_has_internet(self._shell("dumpsys", "connectivity")):
                    self._shell("svc", "data", "enable")
                    self._keep_mobile_until = now + self.HOLD_OFF_S
                    log.warning("%s: Wi-Fi did not hold up without the mobile connection; switched it back on", self.name)
                    return "mobile back on"
                log.info("%s: simulated mobile data switched off (no Android data warnings); Wi-Fi carries on", self.name)
                return "mobile off"
            return "ok"
        except Exception as e:
            log.warning("%s: could not check the connection: %s", self.name, e)
            return "error"

    def boot(self, timeout_s: float = 240, cold: bool = False) -> bool:
        if self.is_available():
            return True
        if not self.is_running():
            exe = Path(self.dcfg.android_sdk) / "emulator" / "emulator.exe"
            args = [str(exe), "-avd", self.avd, "-port", str(self.port), "-no-audio", "-no-boot-anim"]
            from ..avd_settings import gpu_flag
            args += gpu_flag(self.avd)               # config.ini's gpu keys are ignored by the emulator; this is not
            if cold:
                args.append("-no-snapshot-load")     # really boot Android instead of restoring saved state
            if self.headless:
                args.append("-no-window")            # default is a window: a new account has to be signed in by hand
            self._spawn(args)
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.is_available():
                try:
                    if self._shell("getprop", "sys.boot_completed").strip() == "1":
                        log.info("%s booted as %s", self.name, self.serial)
                        self.last_error = self.last_fix = ""
                        self._tune()
                        self.keep_online()           # and again every minute, from is_available()
                        return True
                except Exception:
                    pass
            elif self._proc_exited() and not self.is_running():
                # The emulator program has already gone: waiting out the timeout would only hide why.
                self.last_error, self.last_fix = explain_emulator_failure(self._log_tail())
                log.error("%s could not start: %s", self.name, self.last_error)
                return False
            time.sleep(self.poll_s)
        self.last_error, self.last_fix = f"{self.name} did not finish starting within {int(timeout_s)} seconds.", ""
        log.error(self.last_error)
        return False

    def _tune(self) -> int:
        """Pin the emulator's process to its cores and set its priority."""
        if not self.cpu_affinity and self.priority in ("", "normal"):
            return 0
        try:
            from ..avd_settings import affinity_mask, apply_process_tuning
            n = apply_process_tuning(self.avd, affinity_mask(self.cpu_affinity), self.priority)
            log.info("%s: pinned to [%s], priority %s (%s process)", self.name, self.cpu_affinity or "all",
                     self.priority, n)
            return n
        except Exception as e:
            log.warning("%s: could not apply CPU pinning: %s", self.name, e)
            return 0

    # ---- stop ----
    def power_off(self, timeout_s: float = 60) -> bool:
        """Clean shutdown: ask the emulator to exit over its console (`adb emu
        kill`), which lets it flush the disk image and save its quick-boot state,
        then WAIT until it is really gone. Killing the process is the last resort,
        because that is what corrupts an AVD."""
        if not self.is_running():
            return True
        try:
            self.adb.emu_kill(self.dcfg.android_sdk, self.serial)
        except Exception as e:
            log.warning("%s: emu kill failed: %s", self.name, e)
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if not self.is_running():
                self._reap()
                log.info("%s powered off cleanly", self.name)
                return True
            time.sleep(self.poll_s or 0.01)
        log.warning("%s ignored the shutdown request for %ss; killing its process", self.name, timeout_s)
        self._kill_process()
        time.sleep(self.poll_s)
        self._reap()
        return not self.is_running()

    def stop(self) -> None:                      # older callers (device manager)
        self.power_off()

    def restart(self, timeout_s: float = 240) -> bool:
        """Power off cleanly, then COLD boot. A quick boot only restores the saved
        snapshot, glitch included, so it would not be a restart at all."""
        if not self.power_off():
            return False
        return self.boot(timeout_s=timeout_s, cold=True)

    def _reap(self) -> None:
        if self._proc is not None:
            try:
                self._proc.wait(timeout=20)
            except Exception:
                pass
            self._proc = None

    def _kill_process(self) -> None:
        """Find the qemu process for OUR avd by its command line and end it."""
        script = ("Get-CimInstance Win32_Process -Filter \"Name like 'qemu-system%' or Name = 'emulator.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -match '-avd\\s+{self.avd}(\\s|$)' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=60,
                           creationflags=NO_WINDOW)
        except Exception as e:
            log.warning("%s: could not kill the emulator process: %s", self.name, e)
