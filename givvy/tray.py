"""System tray icon with Pause/Resume/Status/Quit. Runs in its own thread."""
from __future__ import annotations

import os
import threading

from PIL import Image, ImageDraw


def _icon(color):
    img = Image.new("RGB", (64, 64), "black")
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=color)
    d.text((22, 22), "G", fill="black")
    return img


def start_tray(orch, notifier):
    import pystray

    def pause(icon, item):
        orch.paused = True
        icon.icon = _icon("orange")

    def resume(icon, item):
        orch.paused = False
        icon.icon = _icon("limegreen")

    def status(icon, item):
        orch.handle_command("status", orch.device_getter().name if orch.device_getter() else None, len(orch.watcher.states))

    def quit_(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(pystray.MenuItem("Pause", pause), pystray.MenuItem("Resume", resume),
                        pystray.MenuItem("Status → Discord", status), pystray.MenuItem("Quit", quit_))
    icon = pystray.Icon("givvy", _icon("limegreen"), "Whatnot Givvy", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon
