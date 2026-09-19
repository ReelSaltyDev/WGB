"""Settings: everything that would clutter the main window.

  Emulators  cores / RAM / graphics per emulator (the AVD's config.ini), plus CPU
             pinning and priority, which are what actually dedicate resources
  Bot        the handful of numbers worth changing without a text editor
"""
from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import avd_settings as A

log = logging.getLogger(__name__)

GPU_LABELS = {"default": "default  (emulator decides)", "host": "host  (graphics card)", "software": "software  (CPU)"}
PRIORITY_LABELS = {"normal": "normal", "below_normal": "below normal", "idle": "idle", "above_normal": "above normal"}
RAM_CHOICES = ("2048", "3072", "4096", "6144", "8192")


def _key(labels: dict, shown: str) -> str:
    return next((k for k, v in labels.items() if v == shown), shown)


def suggest_pinning(n_emulators: int, cores_each: list[int], logical_cpus: int) -> list[str]:
    """Hand out separate core ranges from the TOP of the CPU down, so the low cores,
    which Windows and games favour, stay free. Blank when it cannot fit."""
    out, top = [], logical_cpus
    for want in cores_each[:n_emulators]:
        lo = top - want
        if lo < 0:
            out.append("")
            continue
        out.append(f"{lo}-{top - 1}" if want > 1 else f"{lo}")
        top = lo
    return out


class SettingsWindow(tk.Toplevel):
    def __init__(self, app, tab: str = ""):
        super().__init__(app)
        self.app, self.engine = app, app.engine
        self.title("Whatnot Givvy - Settings")
        self.transient(app)
        self.resizable(False, False)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.emu_tab = ttk.Frame(nb, padding=12); nb.add(self.emu_tab, text="Emulators")
        self.bot_tab = ttk.Frame(nb, padding=12); nb.add(self.bot_tab, text="Bot")
        self.black_tab = ttk.Frame(nb, padding=12); nb.add(self.black_tab, text="Blacklist")
        from .check_tab import EmulatorCheckTab
        self.check_tab = EmulatorCheckTab(nb, app); nb.add(self.check_tab, text="Emulator check")
        self.nb = nb
        if tab == "check":
            nb.select(self.check_tab)
        self._build_emulators()
        self._build_bot()
        self._build_blacklist()
        bar = ttk.Frame(self); bar.pack(fill="x", padx=10, pady=(0, 10))
        from .version import build_info
        ttk.Label(bar, text=build_info(), foreground="#777").pack(side="left")
        self.update_btn = ttk.Button(bar, text="Check for updates", command=self._check_updates)
        self.update_btn.pack(side="left", padx=12)
        self.update_lbl = ttk.Label(bar, text="", foreground="#555")
        self.update_lbl.pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Apply", command=self._apply).pack(side="right", padx=6)
        self.bind("<Escape>", lambda _e: self.destroy())

    # ------------------------------------------------------------ updates
    def _check_updates(self):
        """Ask GitHub for the newest release. Nothing is downloaded or installed until you say so."""
        from . import updater
        from .version import UPDATE_REPO, __version__
        repo = getattr(self.engine.cfg, "update_repo", "") or UPDATE_REPO
        if not repo:
            self.update_lbl.config(text="Updates are not set up in this build.")
            return
        self.update_btn.config(state="disabled")
        self.update_lbl.config(text="checking...")

        def work():
            try:
                rel = updater.check(repo, __version__)
                self.app.q.put(("call", lambda: self._update_found(rel)))
            except Exception as e:
                self.app.q.put(("call", lambda e=e: self._update_failed(f"could not check: {e}")))
        threading.Thread(target=work, daemon=True).start()

    def _update_failed(self, msg: str):
        if self.winfo_exists():
            self.update_btn.config(state="normal")
            self.update_lbl.config(text=msg[:120])

    def _update_found(self, rel):
        from . import updater
        from .version import __version__
        if not self.winfo_exists():
            return
        self.update_btn.config(state="normal")
        if rel is None:
            self.update_lbl.config(text=f"You have the latest version ({__version__}).")
            return
        self.update_lbl.config(text=f"{rel.version} is available.")
        notes = (rel.notes[:700] + "...") if len(rel.notes) > 700 else rel.notes
        if not updater.can_self_update():
            messagebox.showinfo("Update", f"{rel.version} is available (you have {__version__}).\n\n{notes}\n\n"
                                          f"This copy runs from source, so update it with git. Release page:\n{rel.page}", parent=self)
            return
        if not messagebox.askyesno("Update", f"{rel.version} is available (you have {__version__}).\n\n{notes}\n\n"
                                             f"Download it from GitHub ({rel.size / 1e6:.0f} MB) and install now?\n"
                                             f"The bot closes, updates and reopens. Your settings, history and emulators are kept.",
                                   parent=self):
            return
        self.update_btn.config(state="disabled")

        def work():
            try:
                def progress(done, total):
                    if total:
                        self.app.q.put(("call", lambda d=done, t=total: self.update_lbl.config(
                            text=f"downloading {d / 1e6:.0f} / {t / 1e6:.0f} MB") if self.winfo_exists() else None))
                path = updater.download(rel, progress)
                self.app.q.put(("call", lambda: self._install(path)))
            except Exception as e:
                self.app.q.put(("call", lambda e=e: self._update_failed(f"update failed: {e}")))
        threading.Thread(target=work, daemon=True).start()

    def _install(self, path):
        from . import updater
        self.engine.note(f"installing update {path.name}; the app will close and reopen")
        updater.install_and_restart(path)
        self.app._quit()

    # ------------------------------------------------------------ emulators
    def _emulators(self):
        return [(r, d) for r in self.engine.runners for d in r.devices if hasattr(d, "power_off")]

    def _build_emulators(self):
        f = self.emu_tab
        for w in f.winfo_children():
            w.destroy()
        cpus = os.cpu_count() or 4
        heads = ("Emulator", "CPU cores", "RAM (MB)", "Frame rate", "Graphics", "Pin to cores", "Priority")
        for c, h in enumerate(heads):
            ttk.Label(f, text=h, font=("Segoe UI", 9, "bold")).grid(row=0, column=c, sticky="w", padx=6, pady=(0, 4))
        self.emu_rows = []
        for n, (runner, dev) in enumerate(self._emulators(), start=1):
            try:
                hw = A.read_hw(dev.avd)
            except Exception as e:
                ttk.Label(f, text=dev.name).grid(row=n, column=0, sticky="w", padx=6)
                ttk.Label(f, text=f"cannot read its settings: {e}", foreground="#a00").grid(row=n, column=1, columnspan=5, sticky="w")
                continue
            v = {"cores": tk.StringVar(value=str(hw.cores)), "ram": tk.StringVar(value=str(hw.ram_mb)),
                 "fps": tk.StringVar(value=f"{hw.fps} fps"),
                 "gpu": tk.StringVar(value=GPU_LABELS[hw.gpu]), "pin": tk.StringVar(value=dev.cpu_affinity or ""),
                 "prio": tk.StringVar(value=PRIORITY_LABELS.get(dev.priority or "normal", "normal"))}
            ttk.Label(f, text=f"{dev.name}   ({dev.avd})").grid(row=n, column=0, sticky="w", padx=6, pady=3)
            ttk.Spinbox(f, from_=1, to=min(16, cpus), width=5, textvariable=v["cores"]).grid(row=n, column=1, padx=6)
            ttk.Combobox(f, values=RAM_CHOICES, width=7, textvariable=v["ram"]).grid(row=n, column=2, padx=6)
            ttk.Combobox(f, values=[f"{x} fps" for x in A.FPS_CHOICES], width=7, state="readonly",
                         textvariable=v["fps"]).grid(row=n, column=3, padx=6)
            ttk.Combobox(f, values=list(GPU_LABELS.values()), width=26, state="readonly",
                         textvariable=v["gpu"]).grid(row=n, column=4, padx=6)
            ttk.Entry(f, width=12, textvariable=v["pin"]).grid(row=n, column=5, padx=6)
            ttk.Combobox(f, values=list(PRIORITY_LABELS.values()), width=13, state="readonly",
                         textvariable=v["prio"]).grid(row=n, column=6, padx=6)
            self.emu_rows.append((runner, dev, hw, v))
        last = len(self.emu_rows) + 2
        tools = ttk.Frame(f); tools.grid(row=last, column=0, columnspan=7, sticky="w", pady=(10, 4))
        ttk.Button(tools, text="Suggest separate cores", command=self._suggest).pack(side="left")
        ttk.Button(tools, text="Clear pinning", command=lambda: [v["pin"].set("") for *_x, v in self.emu_rows]).pack(side="left", padx=6)
        ttk.Button(tools, text="Add emulator account...", command=self._add_emulator).pack(side="left", padx=(24, 0))
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong)] + [(f"x{i}", ctypes.c_ulonglong) for i in range(5)]
            ms = MS(); ms.l = ctypes.sizeof(MS); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            ram = f"{ms.total / 2**30:.0f} GB RAM ({ms.avail / 2**30:.0f} GB free)"
        except Exception:
            ram = "RAM unknown"
        help_text = (
            f"This PC: logical processors 0-{cpus - 1}, {ram}.\n\n"
            "CPU cores and RAM are the most an emulator may use, not a reservation.\n"
            "To use less CPU and power: 2 cores and 30 fps. Measured on one emulator playing a live stream: 4 cores/60 fps "
            "2.2 CPUs, 2 cores/30 fps 1.4 CPUs (about a third less), and the bot's screen reads get about 0.2 s slower. A "
            "smaller screen size and Windows' efficiency mode were also measured and changed nothing.\n"
            "Graphics: leave it on 'default'. Measured: the emulator already draws on the graphics card by itself, and an "
            "emulator costs about 2 CPU cores while a live stream plays whatever this is set to. 'software' draws on the CPU "
            "instead and exists only for PCs where an emulator's picture misbehaves.\n"
            "Pin to cores is what really dedicates CPU: e.g. 28-31 keeps that emulator on those four logical processors and "
            "off the rest. Blank = not pinned. Priority 'below normal' makes emulators give way to games.\n\n"
            "Cores, RAM and graphics need that emulator restarted (a cold boot, about 20 s); you are asked after Apply. "
            "Pinning and priority apply immediately, and the bot re-applies them after every boot because Windows forgets them.")
        ttk.Label(f, text=help_text, wraplength=760, justify="left", foreground="#444").grid(
            row=last + 1, column=0, columnspan=7, sticky="w", pady=(8, 0))

    def _suggest(self):
        want = []
        for *_x, v in self.emu_rows:
            try:
                want.append(max(1, int(v["cores"].get())))
            except ValueError:
                want.append(2)
        for (*_x, v), text in zip(self.emu_rows, suggest_pinning(len(want), want, os.cpu_count() or 4)):
            v["pin"].set(text)

    def _add_emulator(self):
        self.app._add_emulator()
        self.after(8000, self._build_emulators)       # the AVD takes a few seconds to create

    # ------------------------------------------------------------ bot
    BOT_FIELDS = (
        ("pacing", "max_entries_per_hour", "Entries per hour, per account", 1, 200,
         "Hard cap. Unconfirmed taps count too."),
        ("scoring", "min_viewers_to_camp", "Smallest stream the scanner will sit in (viewers)", 0, 5000,
         "Favorites are exempt."),
        ("scoring", "favorite_bonus", "Favorite bonus (score points)", 0, 500,
         "60 is enough to beat the usual differences between streams."),
        ("scoring", "odds_w", "Preference for small streams (odds weight, 1 = normal)", 0, 10,
         "1: streams under ~100 viewers all get +30. 3: a tiny stream gets +90, enough to beat a big one."),
        ("scoring", "listed_bonus", "Bonus for streams with giveaways listed (score points)", 0, 500,
         "Streams listing none lose 60; not looked up yet is neutral."),
        ("pacing", "quiet_cooldown_s", "After leaving a dead stream, avoid it for (seconds)", 0, 86400,
         "Stops the scanner going straight back. Favorites are exempt."),
        ("pacing", "switch_after_quiet_s", "Scan mode: leave a stream after this many quiet seconds", 30, 7200, ""),
        ("pacing", "single_quiet_s", "Stay in one stream: leave after this many seconds with no giveaway", 60, 86400,
         "900 = 15 min. 9 in 10 giveaways follow the previous one within 11 min."),
        ("whatnot", "feed_max_pages", "How deep to look into each category (pages of 50 streams)", 1, 12,
         "12 = up to 600 streams per category, smallest included. 1 = only the top 50."),
        ("whatnot", "discovery_interval_s", "Re-check which streams are live every (seconds)", 30, 3600,
         "60 is the default. This is also how the bot notices a stream ended, so longer = slower to move on."),
    )

    def _build_blacklist(self):
        import time
        f = self.black_tab
        for w in f.winfo_children():
            w.destroy()
        ttk.Label(f, text="Sellers the scanner never picks. Added by the 'Blacklist stream' button, by the tariff notice,\n"
                          "or by a giveaway that is only for another country.", justify="left").pack(anchor="w")
        self.black_list = tk.Listbox(f, height=12, width=78, activestyle="none")
        self.black_list.pack(fill="both", expand=True, pady=8)
        self._black_rows = self.engine.store.blacklist_rows()
        for seller, reason, added in self._black_rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(added)) if added else ""
            self.black_list.insert("end", f"{seller:<32} {reason or '':<28} {when}")
        self.black_list.config(font=("Consolas", 9))
        ttk.Button(f, text="Remove from blacklist", command=self._unblacklist).pack(anchor="w")

    def _unblacklist(self):
        sel = self.black_list.curselection()
        if not sel:
            return
        seller = self._black_rows[sel[0]][0]
        self.engine.store.blacklist_remove(seller)
        self.engine.note(f"{seller} removed from the blacklist")
        self._build_blacklist()
        self.app._fill_tree(self.engine.shows)

    def _build_bot(self):
        f = self.bot_tab
        self.bot_vars = {}
        for n, (section, key, label, lo, hi, note) in enumerate(self.BOT_FIELDS):
            cur = getattr(getattr(self.engine.cfg, section), key)
            var = tk.StringVar(value=str(int(cur)))
            ttk.Label(f, text=label).grid(row=n, column=0, sticky="w", pady=4)
            ttk.Spinbox(f, from_=lo, to=hi, width=8, textvariable=var).grid(row=n, column=1, padx=10)
            ttk.Label(f, text=note, foreground="#555").grid(row=n, column=2, sticky="w")
            self.bot_vars[(section, key)] = (var, lo, hi, label)
        ttk.Label(f, text="Saved to config.toml and used straight away; no restart needed.\n"
                          "Everything else (scoring weights, Discord, pacing) is in config.toml next to the program.",
                  foreground="#444", justify="left").grid(row=len(self.BOT_FIELDS), column=0, columnspan=3, sticky="w", pady=(12, 0))

    # ------------------------------------------------------------ apply
    def _apply(self):
        cfg_path = self.engine.cfg.root / "config.toml"
        try:
            bot_changes = []
            for (section, key), (var, lo, hi, label) in self.bot_vars.items():
                try:
                    val = int(var.get())
                except ValueError:
                    raise ValueError(f"{label}: a whole number, please")
                if not lo <= val <= hi:
                    raise ValueError(f"{label}: {lo} to {hi}")
                if val != int(getattr(getattr(self.engine.cfg, section), key)):
                    bot_changes.append((section, key, val))
            emu_changes = []
            for runner, dev, old_hw, v in self.emu_rows:
                try:
                    hw = A.Hardware(int(v["cores"].get()), int(v["ram"].get()), _key(GPU_LABELS, v["gpu"].get()),
                                    int(v["fps"].get().split()[0]))
                except ValueError:
                    raise ValueError(f"{dev.name}: cores and RAM must be whole numbers")
                pin, prio = v["pin"].get().strip(), _key(PRIORITY_LABELS, v["prio"].get())
                A.affinity_mask(pin)                              # validates; raises with a readable message
                if hw.cores < 1 or hw.ram_mb < 1024:
                    raise ValueError(f"{dev.name}: at least 1 core and 1024 MB")
                emu_changes.append((runner, dev, old_hw, hw, pin, prio, v))
        except ValueError as e:
            messagebox.showerror("Settings", str(e), parent=self)
            return

        for section, key, val in bot_changes:
            A.set_config_value(cfg_path, section, key, val)
            setattr(getattr(self.engine.cfg, section), key, val)
            self.engine.note(f"setting {key} = {val}")

        restart = []
        for runner, dev, old_hw, hw, pin, prio, v in emu_changes:
            try:
                if hw != old_hw:
                    A.write_hw(dev.avd, hw)
                    self.engine.note(f"{dev.name}: {hw.cores} cores, {hw.ram_mb} MB, {hw.fps} fps, graphics {hw.gpu}")
                    if dev.is_running():
                        restart.append((runner, dev))
                if pin != (dev.cpu_affinity or "") or prio != (dev.priority or "normal"):
                    A.set_account_keys(cfg_path, dev.name, {"cpu_affinity": pin, "priority": prio})
                    dev.cpu_affinity, dev.priority = pin, prio
                    for a in self.engine.cfg.accounts:
                        if a.name == dev.name:
                            a.cpu_affinity, a.priority = pin, prio
                    self.engine.note(f"{dev.name}: pinned to [{pin or 'all cores'}], priority {PRIORITY_LABELS[prio]}")
                    threading.Thread(target=self._tune_now, args=(dev, pin, prio), daemon=True).start()
            except Exception as e:
                log.exception("applying settings for %s", dev.name)
                messagebox.showerror("Settings", f"{dev.name}: {e}", parent=self)
                return
        self._build_emulators()
        if restart:
            names = ", ".join(d.name for _r, d in restart)
            if messagebox.askyesno("Settings", f"Restart {names} now so the new cores / RAM / graphics take effect?\n\n"
                                               "It is a clean power-off and a cold boot, about 20 seconds each. "
                                               "Your Whatnot sign-in is kept.", parent=self):
                for runner, dev in restart:
                    runner.power(dev.name, "restart")
            else:
                self.engine.note(f"{names}: new hardware settings take effect at the next Restart")

    @staticmethod
    def _tune_now(dev, pin: str, prio: str):
        """Applies to a running emulator immediately. Un-pinning means 'all cores'."""
        try:
            mask = A.affinity_mask(pin) or (1 << min(os.cpu_count() or 1, 64)) - 1
            A.apply_process_tuning(dev.avd, mask, prio)
        except Exception:
            log.exception("could not apply pinning to %s", dev.name)
