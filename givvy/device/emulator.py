"""Start, restart and cleanly power off one Android emulator (AVD)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..config import DeviceCfg, PacingCfg
from . import adb as adb_mod
from .android import AndroidDevice

log = logging.getLogger(__name__)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# What the emulator prints when it cannot start -> (which repair to offer, plain words, where it can be printed).
# It used to be launched with its output thrown away, so a failed start looked like
# 'booting...' forever. The first entry is what a PC that has never run an emulator hits.
# Matched on the emulator's own error lines (_ERROR_LINE) only: every normal start prints lines with no prefix that
# name the hypervisor ('WHPX on Windows 10.0.26200 detected.', 'Windows Hypervisor Platform accelerator is
# operational': all five boot logs of 9/24), and any crash after them read as 'Windows Hypervisor Platform is off'.
# The Visual C++ runtime message is Windows' own text, not the emulator's, so it counts on any line.
_FAILURES = (
    (re.compile(r"multiple emulators with the same AVD|is already running", re.I), "already-running",
     "This emulator is already running in another window.", False),
    (re.compile(r"requires hardware acceleration|WHPX|HAXM|AEHD|hypervisor", re.I), "hypervisor",
     "The emulator cannot use hardware acceleration: Windows Hypervisor Platform is off, or virtualisation is "
     "disabled in the BIOS.", False),
    (re.compile(r"Unknown AVD name|no file .*\.ini", re.I), "setup",
     "This emulator has not been created yet.", False),
    (re.compile(r"VCRUNTIME|MSVCP140|0xc000007b", re.I), "vcredist",
     "Microsoft's Visual C++ runtime is missing, so the emulator program cannot start.", True),
    (re.compile(r"not enough (disk )?space", re.I), "disk",
     "Not enough free disk space to start the emulator.", False),
)
# 'ERROR        | ...', 'FATAL        | ...' (emulator 37), 'PANIC: ...' and 'emulator: ERROR: ...' (older ones)
_ERROR_LINE = re.compile(r"^\s*(?:emulator:\s*)?(ERROR|FATAL|PANIC)\b", re.I)


def explain_emulator_failure(output: str) -> tuple[str, str]:
    """(reason in plain words, repair key) from the emulator's own output.

    Only its error lines are matched (see _FAILURES): every normal start prints 'Ok: Hypervisor compatibility to run
    avd ... are met' on an INFO line and names the accelerator on lines with no prefix, and matching those sent every
    failure, whatever it was, to 'Windows Hypervisor Platform is off' (2026-09-24: an hour and a half of retries blamed
    the hypervisor while the emulator said 'already running'). Nothing known: the last error line, in its own words."""
    lines = (output or "").splitlines()
    errors = [l for l in lines if _ERROR_LINE.match(l)]
    said = [l for l in lines if not l.strip().upper().startswith("INFO")]
    for pattern, fix, reason, anywhere in _FAILURES:
        if any(pattern.search(l) for l in (said if anywhere else errors)):
            return reason, fix
    last = errors[-1].strip() if errors else ""
    last = last.split("|", 1)[1] if "|" in last else _ERROR_LINE.sub("", last).lstrip(": ")
    return (last.strip() or "The emulator closed without saying why."), ""


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

    def _adb_lists_us(self) -> bool | None:
        """Is our emulator in adb's list? None when adb itself failed (adb.list_devices raises then): a broken adb
        server must never be taken for 'the emulator is hung', which would end a healthy one."""
        try:
            return self.serial in self.adb.list_devices(self.dcfg.android_sdk)
        except Exception as e:
            log.warning("%s: adb devices failed: %s", self.name, e)
            return None

    LEFTOVER_RECHECK_S = 5.0   # before ending a leftover, adb is asked again this much later

    def _leftover_unlisted(self) -> bool:
        """adb answered without our emulator, and again LEFTOVER_RECHECK_S later: only then is the process holding our
        AVD a leftover to end. A restarted adb daemon lists a running emulator again within a second or two; the 9/24
        leftovers stayed unlisted for over an hour. An adb that fails is no answer at all (_adb_lists_us None)."""
        if self._adb_lists_us() is not False:
            return False
        time.sleep(self.LEFTOVER_RECHECK_S)
        return self._adb_lists_us() is False

    # ---- start ----
    def _spawn(self, args: list[str]) -> None:
        """Keep what the emulator says. It used to go to DEVNULL, which is why a
        failed start gave no reason at all.

        The last launch's output is copied to emulator-<name>.prev.log first: every retry used to overwrite the log, so
        on 9/24 the stuck instance's own log, the root cause, was gone. Copied, not renamed: a hung emulator still
        holds the file, and Windows refuses to rename it."""
        logs = Path.cwd() / "logs"
        logs.mkdir(exist_ok=True)
        self.log_path = logs / f"emulator-{self.name}.log"
        try:
            if self.log_path.is_file() and self.log_path.stat().st_size > 0:
                shutil.copyfile(self.log_path, logs / f"emulator-{self.name}.prev.log")
        except OSError as e:
            log.warning("%s: could not keep the previous emulator log: %s", self.name, e)
        with self.log_path.open("w", encoding="utf-8", errors="replace") as out:
            out.write(" ".join(args) + "\n")
            out.flush()
            # The emulator gets its own handle to the file; ours is closed once it has started.
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
        args = None                                  # what this call launched with, if it launched anything
        if not self.is_running():
            exe = Path(self.dcfg.android_sdk) / "emulator" / "emulator.exe"
            args = [str(exe), "-avd", self.avd, "-port", str(self.port), "-no-audio", "-no-boot-anim"]
            # After an emulator was ended mid-boot, its next start opened 'Android Emulator closed unexpectedly -
            # send a crash report?' and waited for a click that never came: Android never started, no error was
            # printed, and every retry hung the same way (2026-09-24 04:30-06:10, both accounts down). The emulator
            # also warns that its usage-statistics notice will become a blocking prompt. Neither may ever ask.
            args += ["-crash-report-mode", "disabled", "-no-metrics"]
            from ..avd_settings import gpu_flag
            args += gpu_flag(self.avd)               # config.ini's gpu keys are ignored by the emulator; this is not
            if cold:
                args.append("-no-snapshot-load")     # really boot Android instead of restoring saved state
            if self.headless:
                args.append("-no-window")            # default is a window: a new account has to be signed in by hand
            self._spawn(args)
        cleared = False                              # a leftover was ended in this call: once only
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
                if (self.last_fix == "already-running" and args is not None and not cleared
                        and getattr(self.dcfg, "emulator_kill_leftover", True) and self._leftover_unlisted()):
                    # 2026-09-24 04:30-06:10: emulators left from earlier tries (PIDs 23220 and 51712, in no adb list)
                    # held both AVDs, and every retry died on 'Running multiple emulators with the same AVD' until
                    # someone ended them by hand. adb answered twice and does not have ours, so nothing healthy is
                    # ended: with the daemon down, every account's boot would otherwise end its own running emulator.
                    cleared = True
                    n = self._kill_process()
                    log.warning("%s: an emulator left over from before is holding %s; ending it (%d process(es)) and "
                                "starting clean", self.name, self.avd, n)
                    self._reap()
                    time.sleep(self.poll_s)
                    if "-no-snapshot-load" not in args:
                        args = args + ["-no-snapshot-load"]      # a cold boot: its quick-boot state died with it
                    self._spawn(args)
                    continue
                log.error("%s could not start: %s", self.name, self.last_error)
                return False
            time.sleep(self.poll_s)
        self.last_error, self.last_fix = f"{self.name} did not finish starting within {int(timeout_s)} seconds.", ""
        log.error(self.last_error)
        if (self._proc is not None and self._proc.poll() is None) or self.is_running():
            # Left alone, a stuck emulator keeps its virtual phone locked: the next try either waits on it again or
            # fails with 'already running', for as long as nobody ends it by hand. A stuck one is often not even in
            # adb's list (it hung before Android started), so the process we launched counts too.
            log.warning("%s: ending the stuck emulator so the next start is a clean one", self.name)
            self._kill_process()
            self._reap()
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
        if getattr(self, "reader", None) is not None:
            self.reader.stop()                       # it dies with the emulator anyway; start fresh after the boot
        listed = self._adb_lists_us()
        if listed is None:
            # adb itself failed: whether ours runs cannot be told. is_running() took that for 'not running', and the
            # branch below then ended by force an emulator we had launched, the kill that corrupts an AVD.
            log.warning("%s: adb is not answering; not powering off", self.name)
            self.last_error, self.last_fix = "adb is not answering, so the emulator was left as it is; try again.", ""
            return False
        if not listed:
            # Not in adb's list, but maybe still there: a hung emulator never reaches the list, and Restart and Power
            # off used to return here at once, so on 9/24 neither could end the leftovers holding both AVDs.
            if self._proc is not None and self._proc.poll() is None:
                self._kill_process()                 # one we launched that never came up
                self._reap()
            elif getattr(self.dcfg, "emulator_kill_leftover", True):
                self._kill_process()                 # one left from an earlier run of the app
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

    def _kill_process(self) -> int:
        """Find the qemu process for OUR avd by its command line and end it. Returns how many processes it ended,
        -1 when that could not be told."""
        script = ("$p = @(Get-CimInstance Win32_Process -Filter \"Name like 'qemu-system%' or Name = 'emulator.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -match '-avd\\s+{self.avd}(\\s|$)' }}); "
                  "$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
                  "$p.Count")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=60,
                               creationflags=NO_WINDOW)
            out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
            n = int(out.split()[-1])
        except Exception as e:
            log.warning("%s: could not kill the emulator process: %s", self.name, e)
            return -1
        log.info("%s: ended %d emulator process(es) for %s", self.name, n, self.avd)
        return n
