"""Themes: Light (the Windows look it always had), Dark, and Time Bomb TCG.

Time Bomb TCG takes its colours from timebombtcg.com's stylesheet (read 2026-09-24): background #060b1a, panels
#0c1530 / #111c3d, lines #1e2d57, cream text #f3eddc, muted #94a4c9, gold #e5b544 / #f7d98a, blue #3f7fe0. The site
sets its headings in Cinzel with Georgia as the fallback; Cinzel is not on Windows, so the app uses Georgia.

Light keeps the native Windows theme ('vista'), whose buttons cannot be recoloured; Dark and Time Bomb TCG are built
on ttk's 'clam', which can be. Plain Tk widgets (the activity log, Toplevel windows, combobox drop-down lists) do not
follow ttk styles, so they are recoloured by hand, and Windows is asked for a matching title bar."""
from __future__ import annotations

import ctypes
import logging
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

log = logging.getLogger(__name__)

UI_FONT = "Segoe UI"


@dataclass(frozen=True)
class Theme:
    name: str
    base: str                   # ttk theme underneath: '' = the platform's own
    bg: str                     # window background
    surface: str                # lists, text boxes
    surface2: str               # headings, raised things
    line: str                   # borders
    text: str
    muted: str                  # secondary text
    faint: str                  # the quietest text (version numbers)
    accent: str                 # frame titles, highlights
    button: str
    button_hover: str
    select: str                 # selected row / text
    select_text: str
    good: str
    warn: str
    bad: str
    today: str                  # 'Total Wins Today'
    today_flash: str
    heading_font: str = UI_FONT
    dark_title_bar: bool = False
    caption: str = ""           # Windows 11 title bar colour, when it should not just be light or dark
    caption_text: str = ""
    border: str = ""
    logo: bool = False
    celebrate: bool = False     # a bomb drops and explodes on every win (givvy/blast.py)
    # Win Charts
    chart_bg: str = "white"
    chart_panel: str = "#f1f3f4"
    chart_edge: str = "#dadce0"
    chart_ink: str = "#202124"
    chart_muted: str = "#5f6368"
    chart_grid: str = "#e0e0e0"
    chart_axis: str = "#bdbdbd"
    chart_bars: str = "#4285F4"
    chart_series: tuple = ("#4285F4", "#EA4335", "#FBBC04", "#34A853", "#FF6D01", "#46BDC6", "#7B1FA2", "#9E9D24")


THEMES: dict[str, Theme] = {t.name: t for t in (
    Theme("Light", base="",
          bg="#f0f0f0", surface="#ffffff", surface2="#e6e6e6", line="#c8c8c8", text="#000000", muted="#555555",
          faint="#777777", accent="#000000", button="#e1e1e1", button_hover="#e5f1fb", select="#0078d7",
          select_text="#ffffff", good="#1a7f37", warn="#8a6d00", bad="#b42318", today="#202124",
          today_flash="#1e8e3e"),
    Theme("Dark", base="clam",
          bg="#1e1f22", surface="#2b2d31", surface2="#313338", line="#3f4147", text="#e3e5e8", muted="#a3a6aa",
          faint="#80848e", accent="#e3e5e8", button="#383a40", button_hover="#43454b", select="#3b5bdb",
          select_text="#ffffff", good="#4ec27a", warn="#e0b64d", bad="#f06b6b", today="#e3e5e8",
          today_flash="#4ec27a", dark_title_bar=True,
          chart_bg="#2b2d31", chart_panel="#1e1f22", chart_edge="#3f4147", chart_ink="#e3e5e8",
          chart_muted="#a3a6aa", chart_grid="#3a3c42", chart_axis="#5c5f66", chart_bars="#5b8def",
          chart_series=("#5b8def", "#f06b6b", "#f2c94c", "#4ec27a", "#f2994a", "#56ccf2", "#bb6bd9", "#c0ca33")),
    Theme("Time Bomb TCG", base="clam",
          bg="#060b1a", surface="#0c1530", surface2="#111c3d", line="#1e2d57", text="#f3eddc", muted="#94a4c9",
          faint="#6f7fa6", accent="#e5b544", button="#111c3d", button_hover="#1e2d57", select="#3f7fe0",
          select_text="#ffffff", good="#5fd08a", warn="#f7d98a", bad="#ff7a6b", today="#f7d98a",
          today_flash="#ffffff", heading_font="Georgia", dark_title_bar=True,
          caption="#0c1530", caption_text="#f3eddc", border="#e5b544", logo=True, celebrate=True,
          chart_bg="#0c1530", chart_panel="#060b1a", chart_edge="#1e2d57", chart_ink="#f3eddc",
          chart_muted="#94a4c9", chart_grid="#1e2d57", chart_axis="#2e4178", chart_bars="#e5b544",
          chart_series=("#e5b544", "#3f7fe0", "#ff7a6b", "#5fd08a", "#f7d98a", "#8fb3ff", "#c58af9", "#94a4c9")),
)}
DEFAULT = "Light"
_native: dict = {}              # the platform theme's name, remembered before anything is changed


def get(name: str) -> Theme:
    return THEMES.get(name) or THEMES[DEFAULT]


def _style_labels(style: ttk.Style, t: Theme):
    """Named label styles the windows use instead of hard-coded colours."""
    bg = t.bg
    for name, fg in (("Muted", t.muted), ("Faint", t.faint), ("Body", t.text), ("Good", t.good),
                     ("Warn", t.warn), ("Bad", t.bad), ("Accent", t.accent)):
        style.configure(f"{name}.TLabel", foreground=fg, background=bg)
    style.configure("Today.TLabel", foreground=t.today, background=bg, font=(t.heading_font, 22, "bold"))
    style.configure("TodayFlash.TLabel", foreground=t.today_flash, background=bg, font=(t.heading_font, 22, "bold"))
    style.configure("Head.TLabel", foreground=t.text, background=bg, font=(UI_FONT, 9, "bold"))


def apply_style(root: tk.Misc, t: Theme):
    style = ttk.Style(root)
    _native.setdefault("theme", style.theme_use())
    style.theme_use(t.base or _native["theme"])
    if t.base:
        style.configure(".", background=t.bg, foreground=t.text, fieldbackground=t.surface, bordercolor=t.line,
                        darkcolor=t.bg, lightcolor=t.bg, troughcolor=t.bg, focuscolor=t.accent,
                        selectbackground=t.select, selectforeground=t.select_text, insertcolor=t.text,
                        arrowcolor=t.text, font=(UI_FONT, 9))
        style.map(".", foreground=[("disabled", t.faint)])
        style.configure("TFrame", background=t.bg)
        style.configure("TLabel", background=t.bg, foreground=t.text)
        style.configure("TLabelframe", background=t.bg, bordercolor=t.line, lightcolor=t.bg, darkcolor=t.bg)
        style.configure("TLabelframe.Label", background=t.bg, foreground=t.accent, font=(t.heading_font, 9, "bold"))
        style.configure("TButton", background=t.button, foreground=t.text, bordercolor=t.line,
                        lightcolor=t.button, darkcolor=t.button, focuscolor=t.accent, padding=(8, 3))
        style.map("TButton", background=[("disabled", t.bg), ("pressed", t.select), ("active", t.button_hover)],
                  foreground=[("disabled", t.faint), ("pressed", t.select_text)],
                  bordercolor=[("focus", t.accent)])
        for w in ("TCheckbutton", "TRadiobutton"):
            style.configure(w, background=t.bg, foreground=t.text, indicatorbackground=t.surface,
                            indicatorforeground=t.text, focuscolor=t.bg)
            style.map(w, background=[("active", t.bg)], indicatorbackground=[("selected", t.select),
                                                                             ("active", t.button_hover)])
        style.configure("TEntry", fieldbackground=t.surface, foreground=t.text, insertcolor=t.text)
        style.configure("TSpinbox", fieldbackground=t.surface, foreground=t.text, background=t.button,
                        arrowcolor=t.text)
        style.configure("TCombobox", fieldbackground=t.surface, foreground=t.text, background=t.button,
                        arrowcolor=t.text, bordercolor=t.line, lightcolor=t.surface, darkcolor=t.surface)
        style.map("TCombobox", fieldbackground=[("readonly", t.surface), ("disabled", t.bg)],
                  foreground=[("readonly", t.text), ("disabled", t.faint)],
                  selectbackground=[("readonly", t.surface)], selectforeground=[("readonly", t.text)],
                  background=[("active", t.button_hover)])
        style.configure("Treeview", background=t.surface, fieldbackground=t.surface, foreground=t.text,
                        bordercolor=t.line, lightcolor=t.surface, darkcolor=t.surface)
        style.map("Treeview", background=[("selected", t.select)], foreground=[("selected", t.select_text)])
        style.configure("Treeview.Heading", background=t.surface2, foreground=t.text, bordercolor=t.line,
                        lightcolor=t.surface2, darkcolor=t.surface2, font=(UI_FONT, 9, "bold"))
        style.map("Treeview.Heading", background=[("active", t.button_hover)])
        style.configure("TNotebook", background=t.bg, bordercolor=t.line, lightcolor=t.bg, darkcolor=t.bg)
        style.configure("TNotebook.Tab", background=t.surface, foreground=t.muted, bordercolor=t.line,
                        lightcolor=t.surface, darkcolor=t.surface, padding=(10, 4))
        style.map("TNotebook.Tab", background=[("selected", t.surface2)], foreground=[("selected", t.accent)])
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"{orient}.TScrollbar", background=t.surface2, troughcolor=t.bg, bordercolor=t.bg,
                            lightcolor=t.surface2, darkcolor=t.surface2, arrowcolor=t.muted)
            style.map(f"{orient}.TScrollbar", background=[("active", t.button_hover)])
        style.configure("TProgressbar", background=t.accent, troughcolor=t.surface)
        style.configure("TSeparator", background=t.line)
    _style_labels(style, t)
    # plain Tk widgets made from now on
    for pattern, value in (("*Text.background", t.surface), ("*Text.foreground", t.text),
                           ("*Text.insertBackground", t.text), ("*Text.selectBackground", t.select),
                           ("*Listbox.background", t.surface), ("*Listbox.foreground", t.text),
                           ("*TCombobox*Listbox.background", t.surface), ("*TCombobox*Listbox.foreground", t.text),
                           ("*TCombobox*Listbox.selectBackground", t.select),
                           ("*TCombobox*Listbox.selectForeground", t.select_text),
                           ("*Toplevel.background", t.bg)):
        root.option_add(pattern, value, "interactive")


def dress(widget: tk.Misc, t: Theme):
    """Plain Tk widgets already on screen, from this one down: they ignore ttk styles."""
    try:
        if isinstance(widget, (tk.Tk, tk.Toplevel)):
            widget.configure(background=t.bg)
            title_bar(widget, t)
        elif isinstance(widget, tk.Text):
            widget.configure(background=t.surface, foreground=t.text, insertbackground=t.text,
                             selectbackground=t.select, selectforeground=t.select_text,
                             highlightbackground=t.line, highlightcolor=t.accent)
        elif isinstance(widget, tk.Listbox):
            widget.configure(background=t.surface, foreground=t.text, selectbackground=t.select,
                             selectforeground=t.select_text)
        elif isinstance(widget, ttk.Combobox):            # its drop-down list is a hidden Tk listbox, made once
            try:
                pop = widget.tk.call("ttk::combobox::PopdownWindow", widget)
                widget.tk.call(f"{pop}.f.l", "configure", "-background", t.surface, "-foreground", t.text,
                               "-selectbackground", t.select, "-selectforeground", t.select_text)
            except tk.TclError:
                pass
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        dress(child, t)


# ---------------------------------------------------------------- the Windows title bar
DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR = 20, 34, 35, 36
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF


def _colorref(hex_colour: str) -> int:
    h = hex_colour.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


def title_bar(win: tk.Misc, t: Theme):
    """Dark (or Time Bomb navy) title bars on Windows 10 1809+ / 11. Harmless anywhere else."""
    try:
        dwm = ctypes.windll.dwmapi
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
    except (AttributeError, OSError, tk.TclError):
        return

    def put(attr, value):
        v = ctypes.c_uint(value)
        dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    try:
        put(DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if t.dark_title_bar else 0)
        put(DWMWA_CAPTION_COLOR, _colorref(t.caption) if t.caption else DWMWA_COLOR_DEFAULT)
        put(DWMWA_TEXT_COLOR, _colorref(t.caption_text) if t.caption_text else DWMWA_COLOR_DEFAULT)
        put(DWMWA_BORDER_COLOR, _colorref(t.border) if t.border else DWMWA_COLOR_DEFAULT)
    except Exception:
        log.debug("title bar colours not supported here", exc_info=True)


def apply(root: tk.Misc, name: str) -> Theme:
    """Switch the whole app, open windows included. Returns the theme applied."""
    t = get(name)
    apply_style(root, t)
    dress(root, t)
    return t
