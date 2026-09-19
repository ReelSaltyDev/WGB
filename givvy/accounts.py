"""Several accounts at once, one stream each.

Whatnot allows one entry per person per giveaway. `Claims` is what guarantees two
of our accounts are never in the same stream, so no giveaway can ever receive two
entries from the same person. Ranking alone would not: both campers see the same
list and would pick the same best stream.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .config import AccountCfg, Config, derive_accounts, load

log = logging.getLogger(__name__)


class Claims:
    """show_id -> the account camped there. An account holds at most one stream."""

    def __init__(self):
        self._lock = threading.Lock()
        self._held: dict[str, str] = {}

    def claim(self, show_id: str, who: str) -> bool:
        with self._lock:
            holder = self._held.get(show_id)
            if holder is not None and holder != who:
                return False
            for sid in [s for s, h in self._held.items() if h == who and s != show_id]:
                del self._held[sid]                  # moving on drops the old stream
            self._held[show_id] = who
            return True

    def release(self, who: str) -> None:
        with self._lock:
            for sid in [s for s, h in self._held.items() if h == who]:
                del self._held[sid]

    def holder(self, show_id: str) -> str | None:
        with self._lock:
            return self._held.get(show_id)

    def held_by_others(self, who: str) -> set[str]:
        with self._lock:
            return {s for s, h in self._held.items() if h != who}

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._held)


class TaggedNotifier:
    """Prefix Discord messages with the account, so you can tell who entered what."""

    def __init__(self, inner, tag: str):
        self.inner, self.tag = inner, tag

    def plain(self, msg, *a, **k):
        return self.inner.plain(f"[{self.tag}] {msg}", *a, **k)

    def alert(self, msg, *a, **k):
        return self.inner.alert(f"[{self.tag}] {msg}", *a, **k)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class AccountRunner:
    """One Whatnot login and the devices that can run it.

    Devices sharing a login are alternatives, in order: the docked phone first,
    the emulator when the phone is away. Only one runs at a time.
    """

    def __init__(self, account: str, devices: list, camper, cache_s: float = 5.0):
        self.account = account
        self.devices = list(devices)
        self.camper = camper
        self.enabled = {d.name: True for d in self.devices}
        self.cache_s = cache_s
        self._cached = None
        self._cached_at = 0.0
        self._booting: set[str] = set()
        self.busy: dict[str, str] = {}             # device name -> what it is doing, for the window
        self.on_enabled_changed = None             # callable(): the window saves the tick-boxes
        self.problems: dict[str, tuple[str, str]] = {}    # device name -> (why it would not start, repair key)
        self._failed_at: dict[str, float] = {}
        self.boot_now = False                      # tests: boot on the calling thread
        self.last_tick = 0.0
        self.thread: threading.Thread | None = None

    def set_enabled(self, device_name: str, yes: bool):
        self.enabled[device_name] = yes
        self._cached_at = 0.0

    POWER = {"start": "starting...", "restart": "restarting...", "off": "shutting down..."}

    def power(self, device_name: str, action: str, wait: bool = False) -> None:
        """Start / restart / power off one emulator, off the UI thread.

        Powering off also unticks the device. Otherwise the bot, finding its
        account with nothing to run on, would boot it straight back up.
        """
        dev = next((d for d in self.devices if d.name == device_name), None)
        if dev is None or not hasattr(dev, "power_off") or device_name in self.busy:
            return
        self.busy[device_name] = self.POWER[action]
        self._failed_at.pop(device_name, None)      # you asked: try now, whatever happened before
        self.set_enabled(device_name, action != "off")
        if self.on_enabled_changed:
            self.on_enabled_changed()

        def run():
            try:
                ok = {"start": dev.boot, "restart": dev.restart, "off": dev.power_off}[action]()
                log.info("[%s] %s %s: %s", self.account, action, device_name, "ok" if ok else "FAILED")
                if action != "off":
                    self._note_boot(dev, bool(ok))
                else:
                    self.problems.pop(device_name, None)
                    self._failed_at.pop(device_name, None)
            except Exception:
                log.exception("power %s failed for %s", action, device_name)
            finally:
                self.busy.pop(device_name, None)
                self._cached_at = 0.0

        if wait:
            run()
        else:
            threading.Thread(target=run, daemon=True).start()

    def is_booting(self, device_name: str) -> bool:
        return device_name in self._booting

    def current_device(self):
        """First enabled device that is connected. Cached briefly: every check is
        an `adb devices` call and the camper asks on every tick."""
        now = time.time()
        if self.cache_s and now - self._cached_at < self.cache_s:
            return self._cached
        chosen = None
        for d in self.devices:
            if not self.enabled.get(d.name, True):
                continue
            if self.busy.get(d.name):
                # Seen live: adb answers several seconds before Android has finished booting, and the
                # stream link fired in that gap went nowhere.
                continue
            try:
                if d.is_available():
                    chosen = d
                    break
            except Exception as e:
                log.warning("availability check failed for %s: %s", d.name, e)
        self._cached, self._cached_at = chosen, now
        return chosen

    RETRY_AFTER_S = 300        # a boot that failed is not retried for this long (or until you press Start)

    def _note_boot(self, dev, ok: bool, now: float | None = None) -> None:
        if ok:
            self.problems.pop(dev.name, None)
            self._failed_at.pop(dev.name, None)
        else:
            self.problems[dev.name] = (getattr(dev, "last_error", "") or "The emulator did not start.",
                                       getattr(dev, "last_fix", ""))
            self._failed_at[dev.name] = time.time() if now is None else now

    def ensure_running(self, now: float | None = None):
        """No device up for this login: boot its first enabled emulator, in the
        background, once. Emulators are never shut down by the bot.

        A boot that FAILS is remembered with its reason and left alone for a while.
        Boots now fail in two seconds instead of four minutes, so without this the
        bot would relaunch a broken emulator every tick."""
        now = time.time() if now is None else now
        if self.current_device() is not None:
            return
        for d in self.devices:
            if not (self.enabled.get(d.name, True) and hasattr(d, "boot") and d.name not in self._booting
                    and d.name not in self.busy):
                continue
            if now - self._failed_at.get(d.name, -1e18) < self.RETRY_AFTER_S:
                return                               # known-broken: the window shows why and offers the repair
            self._booting.add(d.name)

            def run(dev=d, now=now):
                ok = False
                try:
                    log.info("[%s] booting emulator %s", self.account, dev.name)
                    ok = bool(dev.boot())
                except Exception:
                    log.exception("boot failed for %s", dev.name)
                finally:
                    self._note_boot(dev, ok, now)
                    self._booting.discard(dev.name)
                    self._cached_at = 0.0

            if self.boot_now:
                run()
            else:
                threading.Thread(target=run, daemon=True).start()
            return


def build_device(a: AccountCfg, cfg: Config, dry_run: bool = False):
    from .device.android import AndroidDevice
    from .device.emulator import EmulatorDevice
    if a.kind == "emulator":
        dev = EmulatorDevice(cfg.device, cfg.pacing, dry_run, name=a.name, avd=a.avd, port=a.port or 5554)
    else:
        dev = AndroidDevice(a.name, a.serial or cfg.device.phone_serial, cfg.device, cfg.pacing, dry_run)
    dev.usernames = {a.whatnot_username} if a.whatnot_username else set()
    dev.cpu_affinity, dev.priority = a.cpu_affinity, a.priority
    return dev


def group_by_account(accounts: list[AccountCfg]) -> dict[str, list[AccountCfg]]:
    out: dict[str, list[AccountCfg]] = {}
    for a in accounts:
        out.setdefault(a.account or a.name, []).append(a)
    return out


# ---------------------------------------------------------------- add-emulator
def has_account_blocks(text: str) -> bool:
    """Real [[accounts]] blocks, not the commented sample the shipped config carries.
    A plain substring test mistook that comment for blocks, which on a fresh install
    broke pinning and renaming and made 'add emulator' drop the original emulator."""
    return re.search(r"(?m)^[ \t]*\[\[accounts\]\]", text) is not None


def next_free_port(cfg: Config) -> int:
    """Emulator console ports are even, 5554 upward; adb serial is emulator-<port>."""
    used = {a.port for a in cfg.accounts if a.kind == "emulator"}
    port = 5554
    while port in used:
        port += 2
    return port


def _block(a: AccountCfg) -> str:
    lines = ["", "[[accounts]]", f'name = "{a.name}"', f'kind = "{a.kind}"', f'account = "{a.account}"']
    if a.kind == "phone":
        lines.append(f'serial = "{a.serial}"')
    else:
        lines += [f'avd = "{a.avd}"', f"port = {a.port}"]
    return "\n".join(lines) + "\n"


def add_emulator_to_config(path: Path | str, name: str) -> AccountCfg:
    """Append an [[accounts]] block for a new emulator. If the file predates
    accounts, first write out the defaults it was implying, so nothing vanishes."""
    path = Path(path)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", name):
        raise ValueError("name: letters, digits, - and _ only")
    cfg = load(path)
    if any(a.name == name for a in cfg.accounts):
        raise ValueError(f"an account device named {name!r} already exists")
    text = path.read_text(encoding="utf-8")
    add = ""
    if not has_account_blocks(text):
        add += "".join(_block(a) for a in derive_accounts(cfg.device))
    new = AccountCfg(name=name, kind="emulator", account=name, avd=f"givvy-{name}", port=next_free_port(cfg))
    add += _block(new)
    path.write_text(text.rstrip("\n") + "\n" + add, encoding="utf-8")
    return new


NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,24}")


def rename_in_config(path: Path | str, old_name: str, new_name: str, new_account: str) -> tuple[str, str]:
    """Rename a device and/or its account label in config.toml, touching nothing
    else in the file. The AVD and port stay as they are. Returns (old_account,
    new_account). Every device on the old account label moves with it: the label
    IS the Whatnot login they share."""
    path = Path(path)
    cfg = load(path)
    dev = next((a for a in cfg.accounts if a.name == old_name), None)
    if dev is None:
        raise ValueError(f"no device named {old_name!r}")
    for label, value in (("device name", new_name), ("account label", new_account)):
        if not NAME_RE.fullmatch(value or ""):
            raise ValueError(f"{label}: 1-24 letters, digits, - or _")
    if any(a.name == new_name for a in cfg.accounts if a is not dev):
        raise ValueError(f"a device named {new_name!r} already exists")
    old_account = dev.account
    if new_account != old_account and any(a.account == new_account for a in cfg.accounts):
        raise ValueError(f"account label {new_account!r} is already used; sharing a label means sharing "
                         f"one Whatnot login")
    text = path.read_text(encoding="utf-8")
    if not has_account_blocks(text):
        text = text.rstrip("\n") + "\n" + "".join(_block(a) for a in derive_accounts(cfg.device))

    out: list[str] = []
    block: list[str] | None = None

    def flush():
        if block is None:
            return
        name_i = next((i for i, l in enumerate(block) if re.match(r"\s*name\s*=", l)), None)
        acc_i = next((i for i, l in enumerate(block) if re.match(r"\s*account\s*=", l)), None)
        name = re.match(r'\s*name\s*=\s*"([^"]*)"', block[name_i]).group(1) if name_i is not None else ""
        acc = re.match(r'\s*account\s*=\s*"([^"]*)"', block[acc_i]).group(1) if acc_i is not None else name
        lines = list(block)
        if acc == old_account and new_account != old_account:
            if acc_i is not None:
                lines[acc_i] = f'account = "{new_account}"'
            elif name_i is not None:
                lines.insert(name_i + 1, f'account = "{new_account}"')
        elif acc_i is None and name == old_name and name_i is not None:
            # the label defaulted to the old name; pin it so renaming the device does not move the account
            lines.insert(name_i + 1, f'account = "{acc}"')
        if name == old_name and name_i is not None:
            lines[name_i] = f'name = "{new_name}"'
        out.extend(lines)

    for line in text.split("\n"):
        if line.strip() == "[[accounts]]":
            flush()
            block = [line]
        elif block is not None and line.lstrip().startswith("["):
            flush()
            block = None
            out.append(line)
        elif block is not None:
            block.append(line)
        else:
            out.append(line)
    flush()
    path.write_text("\n".join(out), encoding="utf-8")
    return old_account, new_account


def create_avd(sdk: str, avd: str, image: str = "system-images;android-35;google_apis_playstore;x86_64") -> None:
    """Same recipe as tools/install_emulator.ps1, for one more AVD."""
    sdk_p = Path(sdk)
    env = dict(os.environ)
    jdk = sdk_p / "jdk"
    if jdk.exists():
        env["JAVA_HOME"] = str(jdk)
    env["ANDROID_SDK_ROOT"] = env["ANDROID_HOME"] = str(sdk_p)
    avdmanager = sdk_p / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat"
    listed = subprocess.run([str(avdmanager), "list", "avd"], capture_output=True, text=True, env=env, timeout=120)
    if f"Name: {avd}\n" in listed.stdout.replace("\r", ""):
        return
    r = subprocess.run([str(avdmanager), "create", "avd", "-n", avd, "-k", image, "-d", "pixel_7"],
                       input="no\n", capture_output=True, text=True, env=env, timeout=600)
    ini = Path.home() / ".android" / "avd" / f"{avd}.avd" / "config.ini"
    if not ini.exists():
        raise RuntimeError(f"avdmanager did not create {avd}: {r.stdout[-400:]} {r.stderr[-400:]}")
    with ini.open("a", encoding="utf-8") as f:
        f.write("hw.keyboard=yes\ndisk.dataPartition.size=8G\n")
    # Measured on three emulators playing live streams: 4 cores/60 fps cost 8.0 logical CPUs and 137 W
    # package power; 2 cores/30 fps cost 4.4 and 116 W, with entry times unchanged (19.3s -> 19.4s).
    from .avd_settings import Hardware, write_hw
    write_hw(avd, Hardware(cores=2, ram_mb=4096, gpu="default", fps=30))
    # Ownership marker. The uninstaller only ever offers to delete emulators that carry it: an emulator
    # that merely has the same name in config.toml may be someone's own (that mistake cost a sign-in).
    (ini.parent / ".created-by-whatnot-givvy").write_text("Created by Whatnot Givvy.", encoding="utf-8")
