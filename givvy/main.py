"""CLI: python -m givvy.main run [--dry-run] | discover | watch | status"""
from __future__ import annotations

import argparse
import time
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import load
from .device.android import AndroidDevice
from .device.emulator import EmulatorDevice
from .device.manager import DeviceManager
from .discovery import Discovery
from .notify import Notifier, format_status
from .orchestrator import Orchestrator
from .store import Store
from .watcher import Watcher
from .whatnot_api import WhatnotClient


def setup_logging(path: str):
    # Show titles contain emoji; Windows consoles default to cp1252 and would raise
    # UnicodeEncodeError on print()/StreamHandler. Force UTF-8 with replacement.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def build(cfg, dry_run: bool):
    client = WhatnotClient()
    store = Store(cfg.root / cfg.paths.db)
    notifier = Notifier(cfg.discord, skip_history=True)
    discovery = Discovery(client, cfg.whatnot.feeds, cfg.whatnot.followed_sellers + store.followed_sellers(), cfg.whatnot.feed_page_size, cfg.whatnot.feed_max_pages)

    def on_event(e):
        logging.getLogger("givvy.watcher").info(
            "%s %s %s %r", e.status, e.show.seller, e.live_product_id[:8], e.prize_text)
        if e.status == "active":
            store.record_giveaway(e.live_product_id, e.show, e.listing, e.started_at)
        else:
            store.end_giveaway(e.live_product_id)

    watcher = Watcher(client, cfg.whatnot.client_version, cfg.whatnot.watch_slots, on_event=on_event)
    phone = AndroidDevice("phone", cfg.device.phone_serial, cfg.device, cfg.pacing, dry_run) if cfg.device.phone_serial else None
    emu = EmulatorDevice(cfg.device, cfg.pacing, dry_run) if cfg.device.avd_name else None
    manager = DeviceManager(phone, emu, cfg.device)
    orch = Orchestrator(cfg, store, watcher, notifier, device_getter=lambda: manager.current, followed=set(cfg.whatnot.followed_sellers))
    return client, store, notifier, discovery, watcher, manager, orch


def main(argv=None):
    ap = argparse.ArgumentParser(prog="givvy")
    ap.add_argument("cmd", choices=["run", "discover", "watch", "status", "blacklist", "unblock", "add-emulator", "accounts"])
    ap.add_argument("seller", nargs="?", help="unblock: the seller to allow again; add-emulator: a short name, e.g. emu2")
    ap.add_argument("--dry-run", action="store_true", help="never tap Enter/Follow; log what would happen")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--no-tray", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "add-emulator":
        from .accounts import add_emulator_to_config, create_avd
        if not a.seller:
            ap.error("add-emulator needs a name, e.g. emu2")
        new = add_emulator_to_config(a.config, a.seller)
        print(f"config: added {new.name} (avd {new.avd}, port {new.port}). Creating the AVD, this takes a minute...")
        create_avd(load(a.config).device.android_sdk, new.avd)
        print(f"done. Restart Whatnot Givvy; it boots {new.name} in a window. Sign in to Play Store and "
              f"Whatnot on it yourself, once.")
        return
    cfg = load(a.config)
    if a.cmd == "accounts":
        for x in cfg.accounts:
            where = x.serial if x.kind == "phone" else f"avd {x.avd}, emulator-{x.port}"
            print(f"{x.account:10} {x.name:12} {x.kind:9} {where}{'' if x.enabled else '   (disabled)'}")
        return
    setup_logging(str(cfg.root / cfg.paths.log))
    client, store, notifier, discovery, watcher, manager, orch = build(cfg, a.dry_run)

    if a.cmd == "discover":
        for s in discovery.poll():
            print(f"{s.game:9} {s.viewers:5} {s.seller:24} {s.title[:60]}")
        return
    if a.cmd == "blacklist":
        rows = store.blacklist_rows()
        for seller, reason, ts in rows:
            print(f"{seller:28} {reason:10} {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}")
        print(f"{len(rows)} blacklisted")
        return
    if a.cmd == "unblock":
        if not a.seller:
            ap.error("unblock needs a seller name")
        print("removed" if store.blacklist_remove(a.seller) else "not on the blacklist", a.seller)
        return
    if a.cmd == "status":
        print(format_status(store.status_summary(24), None, False, 0))
        return
    if a.cmd == "watch":
        watcher.on_event = lambda e: print(e.status, e.show.game, e.show.seller, e.live_product_id[:8], repr(e.prize_text), e.follower_only)

        async def w():
            t = asyncio.create_task(watcher.run())
            while True:
                watcher.set_shows(await asyncio.to_thread(discovery.poll), set(cfg.whatnot.followed_sellers))
                await asyncio.sleep(cfg.whatnot.discovery_interval_s)
        asyncio.run(w())
        return

    # run
    if not a.no_tray:
        from .tray import start_tray
        start_tray(orch, notifier)
    notifier.plain(f"givvy started ({'DRY RUN' if a.dry_run else 'live'})")
    asyncio.run(orch.run(discovery, manager, client))


if __name__ == "__main__":
    main()
