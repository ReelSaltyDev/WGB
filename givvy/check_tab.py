"""Settings > Emulator check: why an emulator will not start, and the repair.

This is also the first-run setup. The installer only copies the program; the
Android emulator and image are downloaded here, from Google, after the person has
read the notices (they may not be redistributed). It opens by itself when a check
fails, so a fresh install lands here instead of on a Start button that does nothing.
"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import doctor

log = logging.getLogger(__name__)
LICENCE_URL = "https://developer.android.com/studio/terms"
RULES_URL = "https://help.whatnot.com/hc/en-us/articles/14349906179597-Giveaway-Rules-Requirements"
NOTICE = (
    "Before the first download, read this.\n"
    "  - Whatnot's rules prohibit automated giveaway entry. Entries can be voided and your account limited or banned. "
    "You are choosing that risk for your own account.\n"
    "  - One entry per person per giveaway: with several accounts the bot never puts two of them in the same stream, and "
    "it does nothing to hide them from Whatnot.\n"
    "  - It never types or stores passwords. You sign in to Google Play and Whatnot inside the emulator yourself, once.\n"
    "  - The Android emulator and image (about 2 GB) are downloaded from Google under Google's licences, which you accept "
    "by continuing.")
MARK = {True: "✔", False: "✖", None: "?"}
FIX_LABEL = {"hypervisor": "Turn it on (admin)", "vcredist": "Install it (admin)", "setup": "Download / create"}


class EmulatorCheckTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app, self.engine = app, app.engine
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.checks: list[doctor.Check] = []

        self.notice = ttk.Frame(self)
        ttk.Label(self.notice, text=NOTICE, wraplength=780, justify="left").pack(anchor="w")
        row = ttk.Frame(self.notice); row.pack(anchor="w", pady=(4, 0))
        ttk.Button(row, text="Whatnot's giveaway rules", command=lambda: webbrowser.open(RULES_URL)).pack(side="left")
        ttk.Button(row, text="Google's Android SDK licence", command=lambda: webbrowser.open(LICENCE_URL)).pack(side="left", padx=8)
        self.agree = tk.BooleanVar(value=bool(app._ui.get("terms_ok")))
        ttk.Checkbutton(self.notice, variable=self.agree, command=self._agreed,
                        text="I understand the risk to my Whatnot account, and I accept Google's Android SDK licences."
                        ).pack(anchor="w", pady=(6, 8))
        if not app._ui.get("terms_ok"):
            self.notice.pack(fill="x")

        self.grid_frame = ttk.Frame(self); self.grid_frame.pack(fill="x", pady=(4, 8))
        bar = ttk.Frame(self); bar.pack(fill="x")
        self.recheck_btn = ttk.Button(bar, text="Check again", command=self.recheck)
        self.recheck_btn.pack(side="left")
        self.test_btn = ttk.Button(bar, text="Test: start the emulator", command=self._test_start)
        self.test_btn.pack(side="left", padx=8)
        self.summary = ttk.Label(bar, text="", font=("Segoe UI", 10, "bold"))
        self.summary.pack(side="left", padx=12)
        self.out = tk.Text(self, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        self.out.pack(fill="both", expand=True, pady=(8, 0))
        self.after(200, self._pump)
        self.recheck()

    # ---- helpers ----
    def _avds(self) -> list[str]:
        return [d.avd for r in self.engine.runners for d in r.devices if hasattr(d, "avd")]

    def _say(self, msg: str):
        self.q.put(("log", msg))

    def _agreed(self):
        self.app._ui["terms_ok"] = bool(self.agree.get())
        self.app._save_ui_state()
        self._draw()

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.out.config(state="normal")
                    self.out.insert("end", payload + "\n"); self.out.see("end")
                    self.out.config(state="disabled")
                elif kind == "checks":
                    self.checks = payload
                    self._draw()
                elif kind == "done":
                    self.busy = False
                    self.recheck()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(200, self._pump)

    # ---- checks ----
    def recheck(self):
        def work():
            try:
                self.q.put(("checks", doctor.run(self.engine.cfg.device.android_sdk, self._avds())))
            except Exception as e:
                self._say(f"check failed: {e}")
        self.summary.config(text="checking...")
        threading.Thread(target=work, daemon=True).start()

    def _draw(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        first = doctor.first_problem(self.checks)
        for n, c in enumerate(self.checks):
            colour = {True: "Good.TLabel", False: "Bad.TLabel", None: "Warn.TLabel"}[c.ok]
            ttk.Label(self.grid_frame, text=MARK[c.ok], style=colour, font=("Segoe UI", 11, "bold")
                      ).grid(row=n, column=0, sticky="nw", padx=(0, 8), pady=2)
            ttk.Label(self.grid_frame, text=c.title, font=("Segoe UI", 9, "bold")).grid(row=n, column=1, sticky="nw", pady=2)
            ttk.Label(self.grid_frame, text=c.detail, wraplength=470, justify="left", style="Body.TLabel"
                      ).grid(row=n, column=2, sticky="w", padx=10, pady=2)
            if c.ok is False and c.fix in FIX_LABEL and c is first:       # one repair at a time, in order
                b = ttk.Button(self.grid_frame, text=FIX_LABEL[c.fix], command=lambda c=c: self._fix(c))
                b.grid(row=n, column=3, sticky="ne", pady=2)
                if self.busy or (c.fix == "setup" and not self.agree.get()):
                    b.config(state="disabled")
        if not self.checks:
            return
        if first is None:
            self.summary.config(text="Everything an emulator needs is in place.", style="Good.TLabel")
        else:
            need = "" if self.agree.get() or first.fix != "setup" else "  (tick the box above first)"
            self.summary.config(text=f"To fix first: {first.title}{need}", style="Bad.TLabel")

    # ---- repairs ----
    def _fix(self, c: doctor.Check):
        if self.busy:
            return
        if c.fix == "hypervisor":
            doctor.enable_hypervisor_elevated()
            messagebox.showinfo("Whatnot Givvy", "Windows is turning the feature on (approve the administrator prompt).\n\n"
                                                 "RESTART YOUR PC afterwards, then open Whatnot Givvy again.", parent=self)
            return
        self.busy = True
        self._draw()
        threading.Thread(target=self._work, args=(c.fix,), daemon=True).start()

    def _work(self, fix: str):
        try:
            if fix == "vcredist":
                ok = doctor.install_vc_redist(self._say)
                self._say("Visual C++ runtime installed." if ok else "Microsoft's installer did not finish; run it again.")
            elif fix == "setup":
                from . import sdk_setup
                from .accounts import create_avd
                sdk = self.engine.cfg.device.android_sdk
                sdk_setup.ensure_sdk(sdk, self._say)
                for avd in self._avds():
                    if not doctor.avd_exists(avd):
                        self._say(f"Creating emulator '{avd}'...")
                        create_avd(sdk, avd)
                self._say("Done.")
        except Exception as e:
            log.exception("repair %s failed", fix)
            self._say(f"FAILED: {e}\nIt is safe to press the button again; it picks up where it left off.")
        finally:
            self.q.put(("done", None))

    def _test_start(self):
        emus = [(r, d) for r in self.engine.runners for d in r.devices if hasattr(d, "boot")]
        if not emus or self.busy:
            return
        runner, dev = emus[0]
        if runner.is_booting(dev.name) or dev.name in runner.busy:
            # Never two boot() calls on one emulator (AccountRunner.power): they share its process and log, and each
            # can end the other's launch.
            self._say(f"{dev.name} is being started or stopped already; try again when that is done.")
            return
        self.busy = True
        runner.busy[dev.name] = "starting..."          # the bot's own boots and the power buttons wait meanwhile
        self._say(f"Starting {dev.name} (its window should appear within a minute)...")

        def work():
            try:
                ok = dev.boot()
                runner._note_boot(dev, bool(ok))
                self._say(f"{dev.name} started and Android finished booting. It works." if ok
                          else f"{dev.name} did NOT start: {dev.last_error}\nFull output: {dev.log_path}")
            except Exception as e:
                self._say(f"start failed: {e}")
            finally:
                runner.busy.pop(dev.name, None)
                self.q.put(("done", None))
        threading.Thread(target=work, daemon=True).start()
