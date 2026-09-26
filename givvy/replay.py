"""Record what the bot sees and does on a real device, and replay it through the real code in tests.

Why: the tests used hand-made fake screens, and this week's real bugs all lived in the gap between those fakes and
the real app (a banner that takes the card's place after an auction, a card still opening when Enter is tapped, a
screen caught mid-change). A recording is the real thing: every screen the bot read, as the exact XML Android
returned, with its time; every command it sent (taps, keys, stream links); and the screenshots taken for the tick.

RECORD   device.record = true in config.toml (or GIVVY_RECORD=1): each device writes
         recordings/<device>/<date-time>/session.jsonl plus the screens, gzip-compressed (~5 KB each).
         Recordings hold other people's usernames and chat: they stay on this PC (recordings/ is not published,
         and neither is tests/).

REPLAY   ReplayDevice plays a recording back to the REAL AndroidDevice code (the same parsing and the same
         decisions), on a virtual clock: sleeps advance the clock instead of waiting, and each read costs the
         time real reads took. It is open-loop: a tap does not change what comes next, the recording does. So a
         replay checks what the bot concludes and does given what really happened; it cannot show what the app
         would have done had the bot tapped somewhere else.
"""
from __future__ import annotations

import bisect
import gzip
import json
import threading
import time as _real_time
from pathlib import Path


# ---------------------------------------------------------------- recording
class Recorder:
    MAX_FRAMES = 20000         # a session stops recording screens beyond this (about 100 MB compressed)

    def __init__(self, root: Path, device: str, now: float | None = None):
        stamp = _real_time.strftime("%Y%m%d-%H%M%S", _real_time.localtime(now or _real_time.time()))
        self.dir = Path(root) / device / stamp
        self._log = None               # opened on the first thing recorded: a device never used leaves no folder
        self._lock = threading.Lock()
        self.frames = 0

    def _open(self) -> None:
        if self._log is None:
            (self.dir / "frames").mkdir(parents=True, exist_ok=True)
            self._log = (self.dir / "session.jsonl").open("a", encoding="utf-8")

    def _write(self, rec: dict) -> None:
        with self._lock:
            self._open()
            self._log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._log.flush()

    def read(self, t: float, xml: str) -> None:
        if self.frames >= self.MAX_FRAMES:
            return
        self.frames += 1
        name = f"frames/{self.frames:06d}.xml.gz"
        with self._lock:
            self._open()
        with gzip.open(self.dir / name, "wt", encoding="utf-8") as f:
            f.write(xml)
        self._write({"t": t, "done": _real_time.time(), "kind": "read", "file": name})

    def command(self, t: float, args: list[str]) -> None:
        self._write({"t": t, "kind": "shell", "args": list(args)})

    def screenshot(self, t: float, png: bytes) -> None:
        name = f"frames/shot-{int(t * 1000)}.png"
        with self._lock:
            self._open()
        (self.dir / name).write_bytes(png)
        self._write({"t": t, "kind": "shot", "file": name})

    def meta(self, t: float, **info) -> None:
        """What the bot knew at that moment (the stream, what it lists): replay needs it to make the same calls."""
        self._write({"t": t, "kind": "meta", "info": info})

    def close(self) -> None:
        with self._lock:
            if self._log is not None:
                self._log.close()


# ---------------------------------------------------------------- reading a recording back
class Session:
    def __init__(self, path: Path):
        self.dir = Path(path)
        self.events = [json.loads(line) for line in (self.dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
                       if line.strip()]
        self.reads = [e for e in self.events if e["kind"] == "read"]
        self.shots = [e for e in self.events if e["kind"] == "shot"]
        self.metas = [dict(e.get("info") or {}, t=e["t"]) for e in self.events if e["kind"] == "meta"]
        self._times = [e["t"] for e in self.reads]

    @property
    def start(self) -> float:
        return self.reads[0]["t"] if self.reads else 0.0

    @property
    def end(self) -> float:
        return self.reads[-1].get("done", self.reads[-1]["t"]) if self.reads else 0.0

    def xml(self, e: dict) -> str:
        with gzip.open(self.dir / e["file"], "rt", encoding="utf-8") as f:
            return f.read()

    def frame_at(self, t: float) -> dict | None:
        """The last screen whose read had FINISHED by time t (what the bot could have known then)."""
        i = bisect.bisect_right(self._times, t) - 1
        while i >= 0 and self.reads[i].get("done", self.reads[i]["t"]) > t:
            i -= 1
        return self.reads[i] if i >= 0 else (self.reads[0] if self.reads else None)

    def shot_near(self, t: float, within: float = 3.0) -> bytes | None:
        best = min(self.shots, key=lambda e: abs(e["t"] - t), default=None)
        if best is None or abs(best["t"] - t) > within:
            return None
        return (self.dir / best["file"]).read_bytes()

    def read_cost(self) -> float:
        """How long a real read took in this recording (median)."""
        d = sorted(e.get("done", e["t"]) - e["t"] for e in self.reads)
        return d[len(d) // 2] if d else 1.5


# ---------------------------------------------------------------- the virtual clock
class Clock:
    def __init__(self, t: float):
        self.now = t

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += max(0.0, float(s))


class _TimeShim:
    """Stands in for the `time` module inside the device and camper code during a replay."""
    def __init__(self, clock: Clock):
        self._c = clock

    def time(self):
        return self._c.time()

    def sleep(self, s):
        self._c.sleep(s)

    def __getattr__(self, name):
        return getattr(_real_time, name)


class virtual_time:
    """with virtual_time(clock): ...  -  sleeps and clock reads in the bot's code follow `clock`."""
    MODULES = ("givvy.device.android", "givvy.camper", "givvy.device.reader", "givvy.scorer")

    def __init__(self, clock: Clock):
        self.clock = clock
        self._saved = {}

    def __enter__(self):
        import importlib
        shim = _TimeShim(self.clock)
        for name in self.MODULES:
            m = importlib.import_module(name)
            self._saved[name] = m.time
            m.time = shim
        return self.clock

    def __exit__(self, *exc):
        import importlib
        for name, t in self._saved.items():
            importlib.import_module(name).time = t
        return False


# ---------------------------------------------------------------- the replay device
class _ReplayAdb:
    """The adb module, answered from the recording. Commands are kept, not run."""
    def __init__(self, dev: "ReplayDevice"):
        self.dev = dev

    def shell(self, sdk, serial, *args, timeout=30):
        self.dev.sent.append((self.dev.clock.now, list(args)))
        return ""

    def exec_out(self, sdk, serial, *args):
        if args[:1] == ("screencap",):
            png = self.dev.session.shot_near(self.dev.clock.now)
            if png is None:
                raise RuntimeError("no screenshot recorded near this moment")
            return png
        return b""

    def list_devices(self, sdk):
        return {self.dev.serial: "device"}


def ReplayDevice(session: Session, clock: Clock, name: str = "replay"):
    """A real AndroidDevice whose screen and adb come from a recording. Built lazily so importing this module
    does not pull in the device layer."""
    from .config import Config
    from .device.android import AndroidDevice

    class _Replay(AndroidDevice):
        def __init__(self):
            cfg = Config()
            super().__init__(name, "replay-1", cfg.device, cfg.pacing, adb=None)
            self.session, self.clock, self.sent, self.reads = session, clock, [], 0
            self.adb = _ReplayAdb(self)
            self.usernames = frozenset()
            self._cost = session.read_cost()

        def _nodes(self):
            from .device import selectors as S
            self._read_at = self.clock.now
            self.clock.sleep(self._cost)               # a read costs what it cost for real
            e = self.session.frame_at(self.clock.now)
            self.reads += 1
            return S.parse_nodes(self.session.xml(e)) if e else []

        def is_available(self):
            return self.clock.now <= self.session.end

        def app_installed(self):
            return True

        def is_user_busy(self, foreground=None):
            return False

        def open_show(self, url):
            self.sent.append((self.clock.now, ["open", url]))
            return "ok"

        def taps(self):
            return [(t, int(a[2]), int(a[3])) for t, a in self.sent if a[:2] == ["input", "tap"]]

    return _Replay()


def replay_camper(session: Session, cfg=None, step_every: float | None = None):
    """Run the real Camper over a recording. Returns (camper, device, store). The stream and what it lists come
    from the recording's meta events; poll cadence defaults to the bot's own."""
    import tempfile
    from .camper import Camper
    from .chooser import ActivityBook
    from .config import Config
    from .giveaway_info import Listed
    from .models import LiveShow
    from .store import Store

    cfg = cfg or Config()
    cfg.scoring.min_viewers_to_camp = 1
    for k in ("tap_delay_min_s", "tap_delay_max_s", "enter_tap_delay_min_s", "enter_tap_delay_max_s"):
        setattr(cfg.pacing, k, 0.0)
    meta = next((m for m in session.metas if m.get("show_id")), {})
    show = LiveShow(meta.get("show_id", "s1"), meta.get("seller", "seller"), "T", "tag/x", meta.get("game", "pokemon"),
                    int(meta.get("viewers", 10)), 0, "pokemon_cards")
    kind = meta.get("kind", "pack_only")
    buyers = int(meta.get("buyers", 0))
    clock = Clock(session.start)
    dev = ReplayDevice(session, clock)
    # The device's own tap delays too: it builds a fresh Config(), so AndroidDevice._pause slept random.uniform(0.8, 2.0)
    # before a badge tap on the virtual clock, every later read shifted, and a replay test passed or failed by chance
    # (the flicker test failed 1 run in 15). Zeroed, a replay has one outcome.
    dev.pacing = cfg.pacing
    names = meta.get("usernames") or []
    dev.usernames = frozenset(n.strip().lower() for n in (names.split(",") if isinstance(names, str) else names) if n)
    store = Store(Path(tempfile.mkdtemp()) / "replay.db")

    class _Quiet:
        def plain(self, *a, **k): pass
        def alert(self, *a, **k): pass
    with virtual_time(clock):
        c = Camper(cfg, store, _Quiet(), lambda: dev, followed=set())
        c.activity = ActivityBook()
        c.listed_lookup = lambda: {show.id: Listed(1, kind == "pack_only", buyers, kind=kind, grade=2)}
        c.live_check = lambda seller, sid: True
        c.set_shows([show])
        every = step_every or cfg.pacing.poll_interval_s
        while clock.now < session.end:
            c.step(now=clock.now)
            clock.sleep(every)
        c.end_turn()                        # the recording ends as Stop does (Engine._camp_loop): the held giveaway
        c.end_stay(now=clock.now)           # keeps its peak count, and the stay's row ends
    return c, dev, store
