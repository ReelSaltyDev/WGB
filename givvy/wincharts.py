"""Win Charts: wins by account (bars) and wins over time (lines), drawn on plain Tk canvases.

The source is the bot's own win history (store.win_history). Every win the bot sees is added as it happens; the
wins from before this panel existed are read back ONCE from givvy.log's win alerts.

Measured 2026-09-23 against the Givvies tracker (Whatnot's own order history):
- Win alerts before 2026-09-17 09:21 EDT came from a detector that read chat messages: 22 alerts, not one of them
  a real win. They are left out.
- After it: 59 alerts, all 59 real. The bot only sees a win while it is watching the stream when the winner is
  shown, so it undercounts (jgoblin22 40 of 50, jswike 19 of 33). Account 3 was never seen at all: its Whatnot
  name is 'saltygoblin', and the bot was waiting for 'reelsalty85 won the giveaway!'.
Charts are drawn by hand rather than with matplotlib, which would add ~40 MB to the installer."""
from __future__ import annotations

import datetime as dt
import re
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

DETECTOR_FIXED = dt.datetime(2026, 9, 17, 13, 21, tzinfo=dt.timezone.utc).timestamp()   # 09:21 EDT
BACKFILL_KEY = "wins_backfilled_from_log"

_WON = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ INFO givvy\.notify: discord: (.*?)WON in (\S+)! ?(.*)$")
_TAG = re.compile(r"\[([^\]]+)\] $")
_URL = re.compile(r"\s*https://www\.whatnot\.com/live/\S*\s*$")
_RENAMED = re.compile(r"renamed \S+ -> \S+ \(account (\S+) -> (\S+)\)")

# Google Sheets' default series colours, so the charts look like the Givvies tab's.
COLORS = ("#4285F4", "#EA4335", "#FBBC04", "#34A853", "#FF6D01", "#46BDC6", "#7B1FA2", "#9E9D24")


# ---------------------------------------------------------------- reading the history back from the log
def log_files(path: Path | str) -> list[Path]:
    """givvy.log and its rotated copies (5 MB each, 3 kept), oldest first."""
    p = Path(path)
    return [x for x in (p.with_name(p.name + f".{i}") for i in (3, 2, 1)) if x.exists()] + ([p] if p.exists() else [])


def wins_from_log(lines, single_account: str = "") -> list[tuple[float, str, str, str]]:
    """(time, account, seller, prize) for every win alert after the detector fix.

    Alerts carry the account's label at the time; a later rename ('account emu2 -> jgoblin22') is followed so a win
    counts for the account it belongs to now. With one account, alerts carry no label: they are single_account's."""
    alerts, renames = [], {}
    for line in lines:
        m = _RENAMED.search(line)
        if m and " INFO " in line:
            renames[m.group(1)] = m.group(2)
            continue
        m = _WON.match(line.rstrip("\n"))
        if not m:
            continue
        tag = _TAG.search(m.group(2))
        account = tag.group(1) if tag else single_account
        if not account:
            continue
        t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        if t < DETECTOR_FIXED:
            continue
        alerts.append((t, account, m.group(3), _URL.sub("", m.group(4)).strip()))

    def now_called(name):
        for _ in range(10):                       # a chain of renames; never loop forever on a cycle
            if name not in renames or renames[name] == name:
                break
            name = renames[name]
        return name
    return [(t, now_called(a), seller, prize) for t, a, seller, prize in alerts]


def backfill(store, log_path: Path | str, single_account: str = "") -> int:
    """Once per install: put the wins from the log into the win history. Returns how many were added."""
    if store.kv_get(BACKFILL_KEY):
        return 0
    def lines():                                  # all files as ONE stream: a rename in an older file applies
        for f in log_files(log_path):             # to the wins logged in the newer ones
            with open(f, encoding="utf-8", errors="replace") as fh:
                yield from fh
    added = 0
    for t, account, seller, prize in wins_from_log(lines(), single_account):
        added += store.add_win(t, account, seller, prize, source="log")
    store.kv_set(BACKFILL_KEY, str(time.time()))
    return added


# ---------------------------------------------------------------- the Givvy Wins tracker (Whatnot's order history)
TRACKER_EVERY_S = 300


def tracker_account(label: str, accounts) -> str:
    """'Jgoblin (#2)' -> 'jgoblin22': the tracker's display label, matched to the bot's account."""
    base = (label or "").split(" (")[0].strip().lower()
    accounts = [a for a in accounts if a]
    if base in accounts:
        return base
    starts = [a for a in accounts if a.lower().startswith(base) or base.startswith(a.lower())]
    return starts[0] if len(starts) == 1 else base


def import_tracker(store, db_path: Path | str, accounts) -> dict[str, int]:
    """Every win in the tracker's database, read-only. Safe to repeat: a win already imported is skipped."""
    import sqlite3
    counts = {"known": 0, "matched": 0, "added": 0}
    labels: dict[str, str] = {}
    src = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        rows = src.execute("SELECT account, won_at, seller, title, order_number FROM wins "
                           "WHERE won_at IS NOT NULL AND won_at != ''").fetchall()
    finally:
        src.close()
    for label, won_at, seller, title, order in sorted(rows, key=lambda r: r[1]):
        try:
            at = dt.datetime.fromisoformat(won_at).timestamp()
        except ValueError:
            continue
        ref = f"order:{order}" if order else f"manual:{label}|{won_at}|{seller}"
        account = tracker_account(label, accounts)
        counts[store.add_tracker_win(ref, at, account, seller or "", title or "")] += 1
        labels[account] = label
    for account, label in labels.items():             # the charts show the sheet's names: 'Jgoblin (#2)'
        if label and store.kv_get(f"label:{account}") != label:
            store.kv_set(f"label:{account}", label)
    store.kv_set("tracker_read_at", str(time.time()))
    return counts


# ---------------------------------------------------------------- what the charts show
# The same four charts as the Givvies tab of the Purchases sheet, in the same order, with the same titles, axis
# titles, series colours (Sheets' defaults) and data labels. Read from its chart specs 2026-09-24.
CHARTS = (
    # key,        type,     title,                         x axis title, y axis title,        height
    ("accounts", "column", "Givvies Won by Account",      "",           "Givvies",           300),
    ("count",    "column", "Count of Date won",           "Date won",   "Count of Date won", 371),
    ("daily",    "line",   "Givvies per Day",             "Date",       "Givvies won",       398),
    ("by_acct",  "line",   "Givvies per Day by Account",  "Date",       "Givvies won",       320),
)


def accounts_in(rows, known=()) -> list[str]:
    """The bot's own accounts first, as configured, even with no wins (a 0 bar says something too); then any
    others in the history (a removed or renamed account) by name."""
    seen = {a for _t, a, *_ in rows}
    return list(known) + sorted(seen - set(known))


def by_account(rows, known=()) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for _t, a, *_ in rows:
        counts[a] = counts.get(a, 0) + 1
    return [(a, counts.get(a, 0)) for a in accounts_in(rows, known)]


def daily(rows, known=()):
    """(labels, totals, {account: counts}) for every day from the first win to the last, quiet days included, as
    the sheet does: a line that skipped empty days would squeeze quiet stretches together."""
    if not rows:
        return [], [], {a: [] for a in known}
    day = lambda t: dt.date.fromtimestamp(t)
    first, last = day(min(r[0] for r in rows)), day(max(r[0] for r in rows))
    days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
    index = {d: i for i, d in enumerate(days)}
    per = {a: [0] * len(days) for a in accounts_in(rows, known)}
    for t, a, *_ in rows:
        per[a][index[day(t)]] += 1
    totals = [sum(v[i] for v in per.values()) for i in range(len(days))]
    return [f"{d.month}/{d.day}" for d in days], totals, per


def midnight(now: float | None = None) -> float:
    """The start of today, local time."""
    t = time.localtime(time.time() if now is None else now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def wins_today(store, now: float | None = None) -> int:
    """Every win since midnight: the bot's own the moment it sees them, and the tracker's as they come in."""
    return store.wins_since(midnight(now))


def nice_top(n: int) -> int:
    """A round number at or above n for the top of the y axis."""
    for top in (1, 2, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150, 200, 250, 300, 400, 500):
        if top >= n:
            return top
    return int(-(-n // 100) * 100)


def grid_step(top: int) -> int:
    for step in (1, 2, 5, 10, 20, 25, 50, 100):
        if top / step <= 6:
            return step
    return max(1, top // 5)


def last_updated(at: float) -> str:
    t = time.localtime(at)
    return f"{t.tm_mon}/{t.tm_mday}/{t.tm_year} {(t.tm_hour % 12) or 12}:{t.tm_min:02d} {'AM' if t.tm_hour < 12 else 'PM'}"


# ---------------------------------------------------------------- drawing, in the look of a Google Sheets chart
FONT = "Segoe UI"
INK, GRID, AXIS, MUTED = "#202124", "#e0e0e0", "#bdbdbd", "#5f6368"


def draw_chart(cv: tk.Canvas, kind: str, title: str, subtitle: str, x_title: str, y_title: str,
               labels: list[str], series: list[tuple[str, list[int], str]], data_labels: bool = False,
               legend: bool = False, pal=None):
    """One chart: 'column' or 'line'; series = [(name, values, colour)]. pal: a themes.Theme (None = Sheets' look)."""
    INK = getattr(pal, "chart_ink", "#202124")
    GRID = getattr(pal, "chart_grid", "#e0e0e0")
    AXIS = getattr(pal, "chart_axis", "#bdbdbd")
    MUTED = getattr(pal, "chart_muted", "#5f6368")
    head = getattr(pal, "heading_font", FONT)
    cv.delete("all")
    w, h = max(cv.winfo_width(), 240), max(cv.winfo_height(), 160)
    cv.create_text(16, 16, text=title, anchor="nw", font=(head, 12), fill=INK)
    top = 44
    if subtitle:
        cv.create_text(16, 38, text=subtitle, anchor="nw", font=(FONT, 9), fill=MUTED)
        top = 64
    bottom = h - 26 - (20 if x_title else 0) - (24 if legend else 0)
    left, right = 62, w - 20
    if y_title:
        cv.create_text(18, (top + bottom) / 2, text=y_title, angle=90, font=(FONT, 9), fill=INK)
    if x_title:
        cv.create_text((left + right) / 2, bottom + 34, text=x_title, font=(FONT, 9), fill=INK)
    values = [v for _n, vals, _c in series for v in vals]
    if not labels or not values:
        cv.create_text((left + right) / 2, (top + bottom) / 2, text="No wins yet", font=(FONT, 10), fill=MUTED)
        return
    ymax = nice_top(max(1, max(values)))
    step = grid_step(ymax)
    y_of = lambda v: bottom - (bottom - top) * v / ymax
    for v in range(0, ymax + 1, step):
        cv.create_line(left, y_of(v), right, y_of(v), fill=AXIS if v == 0 else GRID)
        cv.create_text(left - 8, y_of(v), text=str(v), anchor="e", font=(FONT, 8), fill=MUTED)
    n = len(labels)
    slot = (right - left) / n
    xs = [left + slot * (i + 0.5) for i in range(n)]
    every = max(1, -(-n * 44 // max(1, int(right - left))))   # ~44 px per date, as Sheets thins them out
    for i, lab in enumerate(labels):
        if i % every == 0:
            cv.create_text(xs[i], bottom + 12, text=lab, font=(FONT, 8), fill=MUTED)
    if kind == "column":
        name, vals, color = series[0]
        bar = min(120, slot * 0.62)
        for i, v in enumerate(vals):
            if v:
                cv.create_rectangle(xs[i] - bar / 2, y_of(v), xs[i] + bar / 2, bottom, fill=color, outline="")
            if data_labels:
                cv.create_text(xs[i], y_of(v) - 9, text=str(v), font=(FONT, 9), fill=INK)
    else:
        for name, vals, color in series:
            pts = [c for i, v in enumerate(vals) for c in (xs[i], y_of(v))]
            if len(vals) > 1:
                cv.create_line(*pts, fill=color, width=2)
            else:
                cv.create_oval(xs[0] - 3, y_of(vals[0]) - 3, xs[0] + 3, y_of(vals[0]) + 3, fill=color, outline="")
    if legend:
        items, x = [], 0
        for name, _v, color in series:
            t = cv.create_text(0, 0, text=name, anchor="w", font=(FONT, 9), fill=INK)
            bb = cv.bbox(t)
            items.append((t, color, x))
            x += 16 + (bb[2] - bb[0] if bb else 60) + 18
        x0, y = (w - (x - 18)) / 2, h - 14
        for t, color, off in items:
            cv.create_oval(x0 + off, y - 5, x0 + off + 10, y + 5, fill=color, outline="")
            cv.coords(t, x0 + off + 16, y)


class WinChartsPanel(ttk.LabelFrame):
    """The expandable Win Charts panel on the main window: the Givvies tab's four charts, stacked, scrolling."""

    NOTE = ("From the bot's own win alerts since 9/17/2026 9:21 AM. It only counts a win it saw on screen, "
            "so it can be lower than your Whatnot order history.")
    NOTE_TRACKER = ("From your Givvy Wins tracker (Whatnot's order history), checked every 5 minutes, plus any "
                    "win the bot has seen that the tracker has not picked up yet.")

    def __init__(self, master, store, accounts=lambda: ()):
        super().__init__(master, text="Win Charts")
        self.store, self.accounts = store, accounts
        self.note = ttk.Label(self, text=self.NOTE, style="Muted.TLabel", font=(FONT, 8), wraplength=460,
                              justify="left")
        self.note.pack(side="bottom", fill="x", padx=8, pady=(0, 6))
        body = ttk.Frame(self); body.pack(fill="both", expand=True, padx=6, pady=6)
        self.scroll = ttk.Scrollbar(body, orient="vertical")
        self.view = tk.Canvas(body, highlightthickness=0, yscrollcommand=self.scroll.set, background="#f1f3f4",
                              width=540)
        self.theme = None
        self.scroll.config(command=self.view.yview)
        self.scroll.pack(side="right", fill="y")
        self.view.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.view, background="#f1f3f4")
        self._win = self.view.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvases: dict[str, tk.Canvas] = {}
        for key, _kind, _title, _x, _y, height in CHARTS:
            cv = tk.Canvas(self.inner, background="white", highlightthickness=1, highlightbackground="#dadce0",
                           height=height)
            cv.pack(fill="x", pady=(0, 10))
            self.canvases[key] = cv
        self.view.bind("<Configure>", self._fit)
        self.inner.bind("<Configure>", lambda _e: self.view.config(scrollregion=self.view.bbox("all")))
        self.bind("<Enter>", lambda _e: self.bind_all("<MouseWheel>", self._wheel))
        self.bind("<Leave>", lambda _e: self.unbind_all("<MouseWheel>"))
        self.bind("<Configure>", lambda e: self.note.config(wraplength=max(200, e.width - 30)))
        self._seen = None
        self._job = None

    def set_theme(self, t):
        """Colours from a themes.Theme; redraws."""
        self.theme = t
        self.view.config(background=t.chart_panel)
        self.inner.config(background=t.chart_panel)
        for cv in self.canvases.values():
            cv.config(background=t.chart_bg, highlightbackground=t.chart_edge)
        self._seen = None
        self.redraw()

    def _fit(self, e):
        self.view.itemconfig(self._win, width=e.width)
        self._soon()

    def _wheel(self, e):
        self.view.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

    def _soon(self):
        if self._job is not None:
            self.after_cancel(self._job)
        self._job = self.after(150, self.redraw)

    def _key(self):
        return self.store.win_count(), self.store.kv_get("tracker_read_at"), time.strftime("%Y%m%d")

    def refresh(self):
        """Cheap: redraws only when a win was added, the tracker was read, or the day changed."""
        if self._key() != self._seen:
            self.redraw()

    def redraw(self):
        self._job = None
        if not self.winfo_ismapped():
            return
        self.update_idletasks()
        rows = self.store.win_history()
        known = list(self.accounts())
        # Every chart names an account by its name in the window (John, 2026-09-24), not the tracker's own label
        # ('Jgoblin (#2)'): the same names everywhere, on every PC, with or without a tracker.
        names = {a: a for a in accounts_in(rows, known)}
        pal = self.theme
        series_colors = getattr(pal, "chart_series", COLORS)
        colors = {a: series_colors[i % len(series_colors)] for i, a in enumerate(accounts_in(rows, known))}
        blue = getattr(pal, "chart_bars", COLORS[0])
        items = by_account(rows, known)
        read_at = self.store.kv_get("tracker_read_at")
        title = "Givvies Won by Account" + (f" (Last Updated: {last_updated(float(read_at))})" if read_at else "")
        c = self.canvases
        draw_chart(c["accounts"], "column", title, f"{sum(n for _a, n in items)} total", "", "Givvies",
                   [names[a] for a, _n in items], [("Givvies", [n for _a, n in items], blue)], data_labels=True,
                   pal=pal)
        labels, totals, per = daily(rows, known)
        for key, kind, t, x_title, y_title, _h in CHARTS[1:]:
            if key == "by_acct":
                series = [(names[a], v, colors[a]) for a, v in per.items()]
            else:
                series = [(y_title, totals, blue)]
            draw_chart(c[key], kind, t, "", x_title, y_title, labels, series, legend=key == "by_acct", pal=pal)
        self.note.config(text=self.NOTE_TRACKER if read_at else self.NOTE)
        self._seen = self._key()
