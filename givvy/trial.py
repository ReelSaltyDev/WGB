"""The 1.2.0 trial against the version before (item M, part 2): ONE report that counts a baseline window (the old
version) and the trial window with the same code, from the same state.db, per account and in total, and says in plain
words what the counts can and cannot show yet. Settings > Results shows it; `--trial-report` writes the same text to
trial_report.txt beside the program. It only reads.

John's plan (2026-09-24): run 1.2.0 (items A-E, the crowd hop live, 3 accounts) as a trial against the old version,
keep it if wins/day go up, revert otherwise.

  windows()     the trial runs from its start marker to now: kv trial_start, written by the first Start on this version
                (mark_start), moved by Settings > Results > 'Start a new trial window now', overridden by [trial] start.
                The baseline is the baseline_days before this version's FIRST start (kv trial_first_start, never
                moved), never before the pack-only rules (PACK_RULES_FROM): moving the trial's start never puts this
                version's own days in the baseline. With the Givvy Wins tracker set, both stop an hour before its last
                read (its wins come in late), or earlier at a win the bot saw that the tracker has not confirmed
  numbers()     the same code for both windows, from what both versions write: giveaways (metrics.giveaways: 1.1.x
                wrote 13-19% repeat rows after the 9/23 rebuild, 1.2.0 one per giveaway), wins (win_history,
                tracker-confirmed when the tracker is set; a pack by giveaway_info.grade_title, the rule the bot enters
                by) and expected wins (givvy/crowd.py's two formulas per giveaway, whatever the settings)
  trial_only()  from the stay log (visits), the crowd hop's decisions and the 1.2.0 entry columns: none exist before

What the counts can show, measured on John's DB 9/20-9/24 (pack wins overdispersed 1.35x on 65 four-hour account
blocks, 2026-09-24): a big loss, and whether the mechanisms moved; never the expected +6-9% in wins a day (CAN_SHOW,
detectable(), wins_needed()).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import statistics
import textwrap
import time
from collections import Counter, namedtuple
from dataclasses import dataclass, field, fields

from . import crowd, metrics
from .camper import VOLUNTARY
from .config import TrialCfg
from .giveaway_info import grade_title, set_words
from .hop import LEFT

# The pack-only rules agreed 9/19 went live 9/20: before that only about 49% of wins were packs (93% after), so a
# baseline reaching further back would flatter the trial. The same precedent as wincharts.DETECTOR_FIXED.
PACK_RULES_FROM = dt.datetime(2026, 9, 20, 4, 0, tzinfo=dt.timezone.utc).timestamp()     # 00:00 EDT
BIG_VIEWERS = 70           # 9/19-9/24: streams with 70+ viewers took 18% of stream time and gave 6 of 103 wins
CROWDED_FIRST = 30         # stays whose first read was over 30 entrants: 28% of stream time at 0.10 wins/h
DISPERSION = 1.35          # variance / mean of pack wins on 65 four-hour account blocks 9/20-9/24 (measured 2026-09-24)
BANDS = ("00-06", "06-12", "12-18", "18-24")     # local hours; 06-12 ran 0.158 pack wins per active hour, the rest 0.403
MIN_WINS = 20              # fewer trial pack wins than this: too early for a verdict
Z = 1.96                   # the 95% range
Z_SHOW = 2.8               # 1.96 + 0.84: a change this many standard errors away shows 4 times in 5
GAINS = (0.06, 0.075, 0.09)    # all of 1.2.0's safe fixes together, as estimated 9/24: +6-9% pack wins a day
COUNTED = ("entered", "unverified")
UNKNOWN, ALL = "(unknown)", "all"
TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
Ratio = namedtuple("Ratio", "value low high")          # trial / baseline, with its 95% range


# ---------------------------------------------------------------- the two windows
def _when(value) -> float | None:
    """A [trial] time as epoch seconds: local 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD' (an unquoted TOML date or date-time
    is read too); blank = None; ValueError when unreadable. Read here, never in config.load(): a typo must not stop the
    app starting."""
    if isinstance(value, dt.datetime):
        return value.timestamp() if value.tzinfo else time.mktime(value.timetuple())
    if isinstance(value, dt.date):
        return time.mktime(value.timetuple())
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    for f in TIME_FORMATS:
        try:
            return time.mktime(time.strptime(text, f))
        except ValueError:
            pass
    raise ValueError(text)


def _float(text) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def windows(store, cfg, now: float) -> dict:
    """The two windows as [start, end) epoch seconds.

    trial     T0 = [trial] start, else the stored marker (kv trial_start). T1 = now, [trial] end, or the tracker cut,
              whichever comes first
    baseline  [trial] baseline_start to T0; else the baseline_days before this version's first start (kv
              trial_first_start, the first marker, never moved; T0 when that is earlier), never before PACK_RULES_FROM,
              to that first start. It used to be counted back from T0: re-marked on 9/30 after a 9/25 first start, 71%
              of 'before' was 1.2.0 itself, and the verdict compared 1.2.0 with itself. Time between the first start
              and a later T0 is in neither window
    the tracker cut  (paths.givvy_wins_db set) an hour before the tracker's last read (kv tracker_read_at): its wins come
              in late (3.9-6.8 min on 9/24); or earlier, at a win the bot saw that the tracker has not confirmed
              (tracker_stall). Both windows stop there, as their wins would be missing after it
    No marker: no trial, and the baseline is the baseline_days before now. 'bad' names the [trial] settings that could
    not be read: they are ignored."""
    t, bad = cfg.trial, []

    def read(name):
        value = getattr(t, name, "")
        try:
            when = _when(value)
            if when is not None and when <= 0:          # a year typo in a TOML offset date-time: Windows cannot show it
                raise ValueError(value)
            return when
        except (TypeError, ValueError, OverflowError, OSError):
            bad.append(f"{name} = {value!r}")
            return None

    start, end, base_start = read("start"), read("end"), read("baseline_start")
    days = _float(getattr(t, "baseline_days", None))
    if days is None or not days > 0:
        bad.append(f"baseline_days = {getattr(t, 'baseline_days', None)!r}")
        days = TrialCfg.baseline_days
    marker = _float(store.kv_get("trial_start"))
    first = _float(store.kv_get("trial_first_start"))
    first = marker if first is None else first               # a marker stored before trial_first_start existed
    read_at = _float(store.kv_get("tracker_read_at")) if cfg.paths.givvy_wins_db else None
    t0 = start if start is not None else marker
    anchor = None if t0 is None else (t0 if first is None else min(first, t0))
    if base_start is not None:
        b0, b1 = base_start, now if t0 is None else t0
    elif anchor is None:
        b0, b1 = max(now - days * 86400, PACK_RULES_FROM), now
    else:
        b0, b1 = max(anchor - days * 86400, PACK_RULES_FROM), anchor
    cut = min(now, read_at - 3600) if read_at is not None else now
    stall = tracker_stall(store, read_at, since=b0) if read_at is not None else None
    if stall is not None:
        cut = min(cut, stall[0])
    out = {"marker": marker, "first": first, "anchor": anchor, "from_config": start is not None,
           "tracker_read": read_at, "stall": stall, "bad": bad, "days": days, "baseline_auto": base_start is None,
           "trial": None, "baseline": (b0, max(b0, min(b1, cut)))}
    if t0 is not None:
        t1 = min(cut, end) if end is not None else cut
        out["trial"] = (t0, max(t0, t1))
    return out


def tracker_stall(store, read_at: float, since: float = 0.0) -> tuple[float, str] | None:
    """(at, account) of the oldest win the bot saw on screen (ref NULL), at or after `since`, that the tracker has not
    confirmed although it has had an hour (before read_at - 3600) and that is newer than the newest win it did confirm
    for that account; None when there is none. The tracker confirmed each win 3.9-6.8 min after it (12 of 12 on 9/24),
    but it runs on hand-seeded per-account refresh tokens and can stop, for every account or for one, while
    tracker_read_at keeps moving (every read of its file sets it). Say it stops on day 5 of 9: the trial's confirmed
    pack wins stay at the day-5 count, the headline reads about x0.56 and the verdict 'clearly worse'. Accounts it has
    never confirmed a win for are left out (it may not follow them), and a false win the bot saw stops counting once the
    tracker confirms a later one for that account."""
    newest = store._x("SELECT account, MAX(at) FROM win_history WHERE ref IS NOT NULL AND account IS NOT NULL "
                      "AND account != '' GROUP BY account").fetchall()
    found = None
    for account, confirmed in newest:
        (at,) = store._x("SELECT MIN(at) FROM win_history WHERE ref IS NULL AND account=? AND at>? AND at>=? AND at<?",
                         (account, confirmed, since, read_at - 3600)).fetchone()
        if at is not None and (found is None or at < found[0]):
            found = (at, account)
    return found


# ---------------------------------------------------------------- the trial's start marker
def snapshot(cfg, accounts) -> dict:
    """The settings the trial runs under, stored with its start: Engine.start and the Settings button both take it from
    here. The report warns when the accounts or the crowd hop's switch are different now."""
    return {"accounts": sorted(accounts), "hop_mode": cfg.hop.mode, "hop_max_per_hour": cfg.hop.max_per_hour,
            "pack_upgrade_ratio": cfg.pacing.pack_upgrade_ratio, "upgrade_after_draw": cfg.pacing.upgrade_after_draw,
            "quiet_max_per_h": cfg.scoring.quiet_max_per_h,
            "crowd_from_seen": cfg.scoring.crowd_from_seen, "one_row_per_giveaway": cfg.pacing.one_row_per_giveaway,
            "resume_after_restart": cfg.pacing.resume_after_restart, "breaker": cfg.safety.breaker}


def mark(store, now: float, label: str, settings: dict) -> None:
    """The trial window starts now: kv trial_start, trial_label (the version) and trial_settings (snapshot()). The
    first marker ever is also kept as trial_first_start and never moved: the automatic baseline ends there however
    often the trial is re-marked (windows()). 1.1.x ignores these keys."""
    if store.kv_get("trial_first_start") is None:
        store.kv_set("trial_first_start", store.kv_get("trial_start") or str(now))
    store.kv_set("trial_start", str(now))
    store.kv_set("trial_label", label)
    store.kv_set("trial_settings", json.dumps(settings))


def mark_start(store, cfg, now: float, label: str, settings: dict) -> bool:
    """The first Start on this version marks the trial ([trial] auto_mark). A marker already stored is never moved
    here. True when it marked."""
    if not cfg.trial.auto_mark or store.kv_get("trial_start") is not None:
        return False
    mark(store, now, label, settings)
    return True


def ticked_accounts(cfg) -> list[str]:
    """The accounts with a device ticked, as the window keeps them (ui_state.json beside config.toml, else config's
    enabled): for the report from the command line, which has no window to ask."""
    try:
        saved = json.loads((cfg.root / "ui_state.json").read_text(encoding="utf-8")).get("enabled") or {}
    except Exception:
        saved = {}
    out = []
    for a in cfg.accounts:
        name = a.account or a.name
        if bool(saved.get(a.name, a.enabled)) and name not in out:
            out.append(name)
    return out


# ---------------------------------------------------------------- counted the same way in both windows
@dataclass
class Numbers:
    """One account, or all of them, in one window (numbers())."""
    giveaways: int = 0             # counted: 'entered' or 'unverified', repeat rows merged (metrics.giveaways)
    entry_rows: int = 0            # the raw 'entered' + 'unverified' rows behind them
    active_hours: int = 0          # clock hours with at least one counted giveaway (its first row)
    account_days: float = 0.0      # local dates with one, each weighted by its share inside the window
    wins: int = 0
    pack_wins: int = 0
    expected_v: float = 0.0        # sum of 1 / crowd.from_viewers(viewers) over the counted giveaways...
    viewers_unknown: int = 0       # ...but these, whose stream is not in the shows table
    expected_seen: float = 0.0     # sum of 1 / crowd.from_seen(the count read at entry); none read: left out
    big: int = 0                   # counted giveaways in streams with BIG_VIEWERS+ viewers
    changes: int = 0               # consecutive counted giveaways of one account in different streams
    band_hours: list = field(default_factory=lambda: [0] * len(BANDS))    # active hours by local time band
    band_pack: list = field(default_factory=lambda: [0] * len(BANDS))     # pack wins by local time band
    hours_by_accounts: dict = field(default_factory=dict)   # {accounts with a counted giveaway in that clock hour: hours}
    pack_by_accounts: dict = field(default_factory=dict)    # {the same: pack wins in those hours} (0 = no account)

    def add(self, o: "Numbers") -> None:
        for f in fields(self):
            a, b = getattr(self, f.name), getattr(o, f.name)
            if isinstance(a, list):
                setattr(self, f.name, [x + y for x, y in zip(a, b)])
            elif isinstance(a, dict):
                for k, v in b.items():
                    a[k] = a.get(k, 0) + v
            else:
                setattr(self, f.name, a + b)


def _band(t: float) -> int:
    return time.localtime(t).tm_hour // 6


def _day_share(day: dt.date, since: float, until: float) -> float:
    """How much of a local date lies inside [since, until): 1.0 for a whole day, less at the window's ends. A 23 or 25
    hour day (a clock change) is still one day."""
    start = time.mktime(day.timetuple())
    end = time.mktime((day + dt.timedelta(days=1)).timetuple())
    return max(0.0, min(end, until) - max(start, since)) / (end - start)


def numbers(store, cfg, since: float, until: float) -> dict[str, Numbers]:
    """Per account, plus ALL, for the window [since, until): see Numbers. The same code for the baseline and the trial.
    Viewers come from the shows table (the viewers at our latest join there), the same source in both windows. A win
    with no account is counted under UNKNOWN and in ALL."""
    set_words(cfg.scoring.wanted_words, cfg.scoring.unwanted_words)      # grade as the app does, from the CLI too
    rows = store._x("SELECT e.account, e.show_id, e.result, e.entries_seen, e.entered_at, COALESCE(e.prize,''), "
                    "s.viewers FROM entries e LEFT JOIN shows s ON s.id=e.show_id "
                    "WHERE e.entered_at>=? AND e.entered_at<? ORDER BY e.entered_at, e.id", (since, until)).fetchall()
    out: dict[str, Numbers] = {}
    viewers = {}
    for account, show_id, result, _n, _t, _p, v in rows:
        viewers[show_id] = v
        if result in COUNTED:
            out.setdefault(account, Numbers()).entry_rows += 1
    hours: dict[int, set] = {}             # clock hour -> the accounts with a counted giveaway in it
    days: set = set()
    last: dict[str, str] = {}
    for g in metrics.giveaways([r[:6] for r in rows]):
        if g.result not in COUNTED:
            continue
        n = out.setdefault(g.account, Numbers())
        n.giveaways += 1
        v = viewers.get(g.show_id)
        if v is None:
            n.viewers_unknown += 1
        else:
            n.expected_v += 1 / crowd.from_viewers(v)
            if v >= BIG_VIEWERS:
                n.big += 1
        if g.entries_seen is not None:
            n.expected_seen += 1 / crowd.from_seen(g.entries_seen)
        if last.get(g.account, g.show_id) != g.show_id:
            n.changes += 1
        last[g.account] = g.show_id
        hours.setdefault(int(g.at // 3600), set()).add(g.account)
        days.add((g.account, dt.date.fromtimestamp(g.at)))
    for h, accounts in hours.items():
        for a in accounts:
            n = out[a]
            n.active_hours += 1
            n.band_hours[_band(h * 3600)] += 1
            n.hours_by_accounts[len(accounts)] = n.hours_by_accounts.get(len(accounts), 0) + 1
    for a, day in sorted(days):
        out[a].account_days += _day_share(day, since, until)
    sql = "SELECT at, COALESCE(account,''), COALESCE(prize,'') FROM win_history WHERE at>=? AND at<?"
    if cfg.paths.givvy_wins_db:
        sql += " AND ref IS NOT NULL"          # tracker-confirmed only: Whatnot's order history is the truth
    for at, account, prize in store._x(sql + " ORDER BY at, id", (since, until)).fetchall():
        n = out.setdefault(account or UNKNOWN, Numbers())
        n.wins += 1
        if grade_title(prize) == 2:
            n.pack_wins += 1
            n.band_pack[_band(at)] += 1
            k = len(hours.get(int(at // 3600), ()))
            n.pack_by_accounts[k] = n.pack_by_accounts.get(k, 0) + 1
    total = Numbers()
    for n in out.values():
        total.add(n)
    out[ALL] = total
    return out


# ---------------------------------------------------------------- the comparison
def rate_ratio(wb: int, xb: float, wt: int, xt: float) -> Ratio | None:
    """(wt / xt) / (wb / xb): the trial's rate over the baseline's, W pack wins over an exposure x (account-days or
    active hours), with the 95% range for wins overdispersed by DISPERSION. None when a side has no wins or exposure."""
    if not (wb and wt and xb and xt):
        return None
    rr = (wt / xt) / (wb / xb)
    se = math.sqrt(DISPERSION * (1 / wb + 1 / wt))
    return Ratio(rr, rr * math.exp(-Z * se), rr * math.exp(Z * se))


def time_of_day_ratio(base: Numbers, trial: Numbers) -> Ratio | None:
    """Trial pack wins over what the baseline's rate in each time band predicts for the trial's active hours in that
    band. 06-12 ran 0.158 pack wins per active hour against 0.403 the rest of the day (9/20-9/24): a trial that ran
    more mornings would look worse for nothing. Bands with no baseline hours are left out, their trial wins too. The
    range is rate_ratio's, on the wins in the bands kept."""
    wb = wt = 0
    expected = 0.0
    for b in range(len(BANDS)):
        if base.band_hours[b]:
            wb += base.band_pack[b]
            wt += trial.band_pack[b]
            expected += trial.band_hours[b] * base.band_pack[b] / base.band_hours[b]
    if not (wb and wt and expected):
        return None
    rr = wt / expected
    se = math.sqrt(DISPERSION * (1 / wb + 1 / wt))
    return Ratio(rr, rr * math.exp(-Z * se), rr * math.exp(Z * se))


def _per(x, d):
    return x / d if d else None


def compare(base: Numbers, trial: Numbers) -> dict:
    """Trial over baseline (ALL, usually). The HEADLINE is pack wins per account-day: it counts outages and idle time.
    Per active hour sits beside it, because item A raises uptime whatever C or E do."""
    def plain(b, t):
        return t / b if b and t is not None else None
    return {"per_day": rate_ratio(base.pack_wins, base.account_days, trial.pack_wins, trial.account_days),
            "per_hour": rate_ratio(base.pack_wins, base.active_hours, trial.pack_wins, trial.active_hours),
            "time_of_day": time_of_day_ratio(base, trial),
            "expected_v": plain(_per(base.expected_v, base.active_hours), _per(trial.expected_v, trial.active_hours)),
            "expected_seen": plain(_per(base.expected_seen, base.active_hours),
                                   _per(trial.expected_seen, trial.active_hours))}


def detectable(wb: int, wt: float) -> tuple[float, float] | None:
    """The smallest change up and down that wb baseline and wt trial pack wins can tell from chance (4 times in 5, at
    the 5% level), with wins overdispersed by DISPERSION. detectable(91, 10**9) is about (+0.41, -0.29): against the 91
    pack wins of 9/20-9/24 even an endless trial shows only a change of about +41% or -29%."""
    if not wb or not wt:
        return None
    se = math.sqrt(DISPERSION * (1 / wb + 1 / wt))
    return math.exp(Z_SHOW * se) - 1, math.exp(-Z_SHOW * se) - 1


def wins_needed(effect: float) -> float:
    """Pack wins needed in EACH window for a change of `effect` (0.075 = +7.5%) to show 4 times in 5 at the 5% level:
    about 6,200 at +6%, 4,050 at +7.5%, 2,850 at +9%."""
    return 2 * DISPERSION * (Z_SHOW / math.log(1 + effect)) ** 2


def verdict(trial_pack: int, headline: Ratio | None) -> str:
    """From the 95% range of the headline ratio (pack wins per account-day)."""
    if trial_pack < MIN_WINS:
        return "too early"
    if headline is None:
        return "nothing to compare with"            # the baseline has no pack wins
    if headline.low > 1:
        return "clearly better"
    if headline.high < 1:
        return "clearly worse: consider reverting"
    return "can't tell apart from chance yet"


def _about(x: float) -> str:
    """3 significant figures, with thousands commas: 4047 -> '4,050'."""
    return f"{round(x, 2 - int(math.floor(math.log10(x)))):,.0f}" if x > 0 else "0"


def can_show(wb: int, wt: int) -> list[str]:
    """The two plain sentences, from the current counts."""
    d = detectable(wb, wt)
    if d is None:
        first = (f"With {wb} pack wins before and {wt} in the trial, no change of any size can be told from chance "
                 f"yet.")
    else:
        first = (f"With these counts ({wb} pack wins before, {wt} in the trial) only a change bigger than "
                 f"{d[0]:+.0%} / {d[1]:+.0%} would show.")
    need = [wins_needed(g) for g in GAINS]
    second = (f"The expected +6-9% needs about {_about(need[0])} (+6%), {_about(need[1])} (+7.5%) or {_about(need[2])} "
              f"(+9%) pack wins in EACH window, and the baseline has {wb}, ")
    endless = math.exp(Z_SHOW * math.sqrt(DISPERSION / wb)) - 1 if wb else math.inf
    if endless >= GAINS[1]:
        second += "so this comparison cannot show a gain that small however long the trial runs."
    else:
        more = 1 / ((math.log(1 + GAINS[1]) / Z_SHOW) ** 2 / DISPERSION - 1 / wb)
        second += f"so +7.5% would show once the trial has about {_about(more)} pack wins."
    return [first, second]


# ---------------------------------------------------------------- only 1.2.0 records these
@dataclass
class Stays:
    """One account, or all of them, in one window (trial_only()): from the stay log (visits), the crowd hop's decisions
    (hop_decisions), the safety pauses (errors) and the 1.2.0 entry columns. The version before wrote none of them."""
    visits: int = 0
    seconds: float = 0.0           # in a stream: stays clipped to the window (an open one counts to its seen_at)
    big_s: float = 0.0             # ...of it in stays joined at BIG_VIEWERS+ viewers
    crowded_s: float = 0.0         # ...in stays whose first read was over CROWDED_FIRST entrants
    joins: int = 0                 # stream links fired (opened = 1), the failed ones too
    voluntary: int = 0             # stays left for a VOLUNTARY reason (a pack-only upgrade, a crowd hop)
    most_in_an_hour: int = 0       # the most of those in one account clock-hour (the crowd hop's cap: 3 an hour)
    failed_opens: int = 0
    reasons: Counter = field(default_factory=Counter)      # why the stays that ended in the window ended
    after_stop: int = 0            # 'app stopped' stays followed by another stay...
    back_after_stop: int = 0       # ...in the same stream (item B)
    decisions: Counter = field(default_factory=Counter)    # crowd-hop rows by (action, reason)
    left: list = field(default_factory=list)               # (crowd, best_crowd) on 'left' rows
    breaker_trips: int = 0
    proven: Counter = field(default_factory=Counter)       # counted entry rows by how they were proven
    peak_ratio: list = field(default_factory=list)         # peak_entries / entries_seen, per entry row

    def add(self, o: "Stays") -> None:
        for f in fields(self):
            a, b = getattr(self, f.name), getattr(o, f.name)
            setattr(self, f.name, max(a, b) if f.name == "most_in_an_hour" else a + b)


def trial_only(store, since: float, until: float) -> dict[str, Stays]:
    """Per account, plus ALL, for the window [since, until): see Stays."""
    out: dict[str, Stays] = {}
    hourly: Counter = Counter()
    order: dict[str, list] = {}
    for account, _seller, show_id, viewers, _kind, first, joined, end, reason, opened in store.visits(since, until):
        s = out.setdefault(account, Stays())
        s.visits += 1
        secs = max(0.0, min(end, until) - max(joined, since))
        s.seconds += secs
        if viewers is not None and viewers >= BIG_VIEWERS:
            s.big_s += secs
        if first is not None and first > CROWDED_FIRST:
            s.crowded_s += secs
        if since <= joined < until:
            s.joins += 1 if opened else 0
            s.failed_opens += 1 if reason == "open_failed" else 0
        ended = reason is not None and since <= end < until
        if ended:
            s.reasons[reason] += 1
            if reason in VOLUNTARY:
                s.voluntary += 1
                hourly[(account, int(end // 3600))] += 1
        order.setdefault(account, []).append((show_id, reason if ended else None))
    for (account, _hour), n in hourly.items():
        out[account].most_in_an_hour = max(out[account].most_in_an_hour, n)
    for account, stays in order.items():
        for (show_id, reason), (next_show, _r) in zip(stays, stays[1:]):
            if reason == "app stopped":
                out[account].after_stop += 1
                out[account].back_after_stop += 1 if next_show == show_id else 0
    for r in store.hop_rows(since, until):
        s = out.setdefault(r["account"], Stays())
        s.decisions[(r["action"], r["reason"])] += 1
        if r["action"] == LEFT:
            s.left.append((r["crowd"], r["best_crowd"]))
    for (message,) in store._x("SELECT message FROM errors WHERE place='breaker' AND ts>=? AND ts<?",
                               (since, until)).fetchall():
        m = re.search(r"(\S+) is paused;", message or "")          # Camper._trip_breaker's alert text
        out.setdefault(m.group(1) if m else UNKNOWN, Stays()).breaker_trips += 1
    for account, result, by, peak, seen in store._x(
            "SELECT account, result, confirmed_by, peak_entries, entries_seen FROM entries WHERE entered_at>=? "
            "AND entered_at<? AND result IN ('entered','unverified') ORDER BY entered_at, id", (since, until)).fetchall():
        s = out.setdefault(account, Stays())
        s.proven[by or ("never" if result == "unverified" else "not recorded")] += 1
        if peak is not None and seen:
            s.peak_ratio.append(peak / seen)
    total = Stays()
    for s in out.values():
        total.add(s)
    out[ALL] = total
    return out


# ---------------------------------------------------------------- the report
WIDTH = 110                # the Settings window's text box
LABEL, CELL = 44, 8

CAN_SHOW = (
    "The baseline held about 91 tracker pack wins on 9/24 (about 105-110 by a 9/25 start). Against it even an endless "
    "trial can only tell a change of about +37-41% or -27-29% from chance. After 14 days with 3 accounts (about 25 "
    "pack wins a day, about 350 wins) only about +43% / -30% shows.",
    "Telling the expected +6-9% from chance on real wins needs about 2,850 (+9%), 4,050 (+7.5%) or 6,200 (+6%) pack "
    "wins in EACH window: 115-375 days per window with 3 or 2 accounts. Switching the crowd hop on and off by turns is "
    "no faster: both arms draw on the same wins.",
    "The crowd yardstick (expected wins per hour, CV 0.31 per account-day) would need about 100 days with 3 accounts "
    "for +7.5%, and it is partly circular for C and E: it rises by construction when the bot sits in smaller streams.",
    "So the trial is a check for a big loss, plus a check that the mechanisms worked: 70+ share down, crowded-stay "
    "share down, expected wins per hour up, observed/expected near 1, voluntary moves at most 3 per hour, failed opens "
    "flat. It cannot confirm +6-9% in wins a day.",
)
RULE = ("Suggested rule: keep 1.2.0 after at least 14 days if this report does not say 'clearly worse' and the "
        "mechanism numbers moved as intended; revert on 'clearly worse'.")
CONFOUNDS = (
    "jswike is back only in the trial (the baseline has it 9/20-9/22 only): 3 accounts compete for the same small "
    "streams, so per-account rates may drop whatever the code does. Hence the split by how many accounts entered in "
    "each clock hour. Never compare total wins per day across the windows: the number of accounts differs.",
    "Time of day: 06-12 ran 0.158 pack wins per active hour against 0.403 the rest of the day (9/20-9/24). Hence the "
    "ratio with the time of day evened out.",
    "Item A (emulator recovery) raises uptime whatever C or E do: hence per active hour beside per account-day.",
    "The tracker lags: the windows stop an hour before its last read, and its latest win is printed at the top.",
)
NO_DATA = ("1.2.0 must run against the SAME state.db the old version wrote, with paths.givvy_wins_db set: either check "
           "out win-more in the live folder, or point paths.db, paths.log and paths.givvy_wins_db at the old folder's "
           "absolute paths. With a fresh state.db the baseline is in a different file and nothing can be compared. The "
           "two versions can never run at the same time (both bind lock port 47615).")

# (label, value from one Numbers), per account, before | trial
ROWS = (
    ("active hours (an entry in the clock hour)", lambda n: _fmt(n.active_hours, "d")),
    ("account-days (dates with an entry)", lambda n: _fmt(n.account_days, ".2f")),
    ("giveaways entered (repeat rows merged)", lambda n: _fmt(n.giveaways, "d")),
    ("  raw entry rows", lambda n: _fmt(n.entry_rows, "d")),
    ("  rows per entry (one row each: 1.00)", lambda n: _fmt(_per(n.entry_rows, n.giveaways), ".2f")),
    ("entries per active hour", lambda n: _fmt(_per(n.giveaways, n.active_hours), ".1f")),
    ("wins", lambda n: _fmt(n.wins, "d")),
    ("pack wins", lambda n: _fmt(n.pack_wins, "d")),
    ("pack wins per active hour", lambda n: _fmt(_per(n.pack_wins, n.active_hours), ".3f")),
    ("pack wins per account-day", lambda n: _fmt(_per(n.pack_wins, n.account_days), ".2f")),
    ("expected wins, from viewers", lambda n: _fmt(n.expected_v, ".1f")),
    ("expected wins, from the count at entry", lambda n: _fmt(n.expected_seen, ".1f")),
    ("  per active hour, from viewers", lambda n: _fmt(_per(n.expected_v, n.active_hours), ".3f")),
    ("  per active hour, from the count", lambda n: _fmt(_per(n.expected_seen, n.active_hours), ".3f")),
    ("pack wins / expected (from viewers)", lambda n: _fmt(_per(n.pack_wins, n.expected_v), ".2f")),
    ("all wins / expected (from viewers)", lambda n: _fmt(_per(n.wins, n.expected_v), ".2f")),
    ("entries in 70+ viewer streams", lambda n: _fmt(_per(n.big, n.giveaways - n.viewers_unknown), ".0%")),
    ("stream changes per active hour", lambda n: _fmt(_per(n.changes, n.active_hours), ".2f")),
    ("entries whose viewers are unknown", lambda n: _fmt(n.viewers_unknown, "d")),
)
# (label, value from one Stays): the labels carry the 9/24 analysis values, for the version before
STAY_ROWS = (
    ("hours in a stream", lambda s: _fmt(s.seconds / 3600, ".1f")),
    ("  in 70+ viewer streams (9/24: 18%)", lambda s: _fmt(_per(s.big_s, s.seconds), ".0%")),
    ("  in stays first read over 30 (9/24: 28%)", lambda s: _fmt(_per(s.crowded_s, s.seconds), ".0%")),
    ("joins per hour in a stream (9/24: about 2)", lambda s: _fmt(_per(s.joins, s.seconds / 3600), ".2f")),
    ("voluntary moves per hour in a stream", lambda s: _fmt(_per(s.voluntary, s.seconds / 3600), ".2f")),
    ("  the most in one clock hour (cap 3)", lambda s: _fmt(s.most_in_an_hour, "d")),
    ("failed opens (9/16-9/24: about 57 in 8 days)", lambda s: _fmt(s.failed_opens, "d")),
    ("  per hour in a stream", lambda s: _fmt(_per(s.failed_opens, s.seconds / 3600), ".2f")),
    ("crowd hops (left after a draw)", lambda s: _fmt(len(s.left), "d")),
    ("  per hour in a stream", lambda s: _fmt(_per(len(s.left), s.seconds / 3600), ".2f")),
    ("safety pauses (breaker trips)", lambda s: _fmt(s.breaker_trips, "d")),
    ("back in the same stream after 'app stopped'",
     lambda s: f"{s.back_after_stop}/{s.after_stop}" if s.after_stop else "-"),
)


def _fmt(x, spec: str) -> str:
    return "-" if x is None else format(x, spec)


def _t(t: float | None, weekday: bool = False) -> str:
    return time.strftime(("%a " if weekday else "") + "%Y-%m-%d %H:%M", time.localtime(t)) if t is not None else "-"


def _wrap(text: str, first: str = "", rest: str = "  ") -> list[str]:
    return textwrap.wrap(text, WIDTH, initial_indent=first, subsequent_indent=rest, break_on_hyphens=False)


def _order(*windows) -> list[str]:
    """Our accounts by name, then UNKNOWN when it has anything, then ALL."""
    names = set().union(*[w.keys() for w in windows if w])
    return sorted(names - {ALL, UNKNOWN}) + ([UNKNOWN] if UNKNOWN in names else []) + [ALL]


def _grid(title: str, accounts: list, rows, before: dict, after: dict | None, empty, show_before: bool = True,
          show_after: bool = True) -> list[str]:
    """One value per account and window, before | trial side by side; a window not shown is '-'."""
    width = 2 * CELL
    out = [title, " " * LABEL + "".join(f"{a[:width - 1]:>{width}}" for a in accounts),
           " " * LABEL + f"{'before':>{CELL}}{'trial':>{CELL}}" * len(accounts)]
    for label, value in rows:
        cells = []
        for a in accounts:
            cells.append(value(before.get(a) or empty()) if show_before else "-")
            cells.append(value((after or {}).get(a) or empty()) if show_after and after is not None else "-")
        out.append(f"{label:<{LABEL}}" + "".join(f"{c:>{CELL}}" for c in cells))
    return out


def _split(title: str, rows, show_after: bool) -> list[str]:
    """rows: (label, hours before, pack wins before, hours in the trial, pack wins in the trial)."""
    part = f"{'hours':>8}{'pack wins':>11}{'per hour':>10}"
    out = [title, f"{'':<24}{'---------- before ----------':>29}   {'---------- trial -----------':>29}",
           f"{'':<24}{part}   {part}"]
    for label, bh, bw, th, tw in rows:
        line = f"{label:<24}{bh:>8}{bw:>11}{_fmt(_per(bw, bh), '.3f'):>10}   "
        line += f"{th:>8}{tw:>11}{_fmt(_per(tw, th), '.3f'):>10}" if show_after else f"{'-':>8}{'-':>11}{'-':>10}"
        out.append(line)
    return out


def _value(v) -> str:
    return ", ".join(map(str, v)) if isinstance(v, list) else str(v)


def _settings_text(s: dict) -> str:
    return "; ".join(f"{k} {_value(v)}" for k, v in s.items())


def _stay_lines(name: str, s: Stays) -> list[str]:
    """The trial-only breakdowns that do not fit a table, for one window (all accounts). How entries were proven and
    their peak counts exist only from 1.2.0: shown for the trial alone, never compared."""
    out = []
    if s.reasons:
        out += _wrap("why stays ended: " + ", ".join(f"{k} {v}" for k, v in s.reasons.most_common())
                     + " ('moved' = no reason recorded: should be rare)", f"  {name}: ", "    ")
    if s.decisions:
        out += _wrap("crowd-hop decisions: " + ", ".join(f"{a}/{r} {v}" for (a, r), v in s.decisions.most_common()),
                     f"  {name}: ", "    ")
    here = [c for c, _b in s.left if c is not None]
    there = [b for _c, b in s.left if b is not None]
    if here and there:
        out.append(f"  {name}: on 'left' rows the expected crowd was a median {statistics.median(here):.0f} where it "
                   f"was and {statistics.median(there):.0f} where it went")
    if s.proven and name == "trial":
        ratio = (f"; the peak entrant count was a median {statistics.median(s.peak_ratio):.2f}x the count at entry "
                 f"(2.27x measured 9/24)" if s.peak_ratio else "")
        out += _wrap("how entries were proven: " + ", ".join(f"{k} {v}" for k, v in s.proven.most_common()) + ratio,
                     f"  {name}: ", "    ")
    return out


def _empty_trial(w: dict) -> str:
    """Why the trial window is empty with the right database."""
    if w.get("stall"):
        return "The trial window is empty: the tracker stopped confirming wins before it began (see the WARNING above)."
    if w["tracker_read"] is not None and w["tracker_read"] - 3600 <= w["trial"][0]:
        return ("The trial window opens an hour after its start: the tracker's wins come in late, so both windows stop "
                "an hour before its last read.")
    return "The trial window is empty: [trial] end is not after its start."


def format_trial(w: dict, base: dict, trial: dict | None, base_stays: dict, trial_stays: dict | None, *, now: float,
                 label: str = "", then: dict | None = None, current: dict | None = None, latest=None,
                 tracker: bool = True, db: str = "") -> str:
    """The report as text (see trial_report). `then`: the settings stored with the marker; `current`: the same now;
    `latest`: the latest tracker win (at, account, seller, prize)."""
    then, current = then or {}, current or {}
    out = ["TRIAL REPORT: this version against the one before, both windows counted by the same code from one state.db",
           f"written {_t(now)}"] + ([f"database {db}"] if db else []) + [""]
    b0, b1 = w["baseline"]
    anchor = w.get("anchor")
    how = ("[trial] baseline_start" if not w["baseline_auto"] else
           f"the {w['days']:g} days before now" if w["trial"] is None else
           f"{w['days']:g} days before this version's first start" if anchor is not None and anchor < w["trial"][0] else
           f"{w['days']:g} days before the trial")
    out.append(f"Before   {_t(b0, True)} -> {_t(b1, True)}  {(b1 - b0) / 86400:5.2f} days   {how}")
    if w["baseline_auto"] and b0 == PACK_RULES_FROM:
        out.append(f"         never before {_t(PACK_RULES_FROM)}, when the pack-only rules went live")
    if w["trial"] is None:
        out.append("Trial    none started: the first Start of this version marks it ([trial] auto_mark), or set [trial] "
                   "start")
    else:
        t0, t1 = w["trial"]
        how = "[trial] start in config.toml" if w["from_config"] else "the marker"
        out.append(f"Trial    {_t(t0, True)} -> {_t(t1, True)}  {(t1 - t0) / 86400:5.2f} days   {how}")
    if w["marker"] is not None:
        out.append(f"Marker   {_t(w['marker'])}   {label or '(no version stored)'}"
                   + ("   ([trial] start in config.toml wins over it)" if w["from_config"] else ""))
        if w.get("first") is not None and w["first"] != w["marker"]:
            out += _wrap(f"First    {_t(w['first'])}: this version's first start. The automatic baseline ends there, "
                         f"however often the trial is re-marked; the time since then before the trial's start is in "
                         f"neither window.", "", "         ")
    if not tracker:
        out += _wrap("Tracker  not set ([paths] givvy_wins_db is blank): the wins are only those the bot saw on screen, "
                     "and it misses some.", "", "         ")
    elif w["tracker_read"] is None:
        out.append("Tracker  set, but not read yet: the windows run to now.")
    else:
        out.append(f"Tracker  last read {_t(w['tracker_read'])}: both windows stop an hour before it (its wins come in "
                   f"late)")
        if latest:
            at, account, seller, prize = latest
            out.append(f"         latest tracker win {_t(at)}  {account or UNKNOWN}  {seller}  {prize}"[:WIDTH])
        if w.get("stall"):
            at, account = w["stall"]
            out += _wrap(f"the tracker has not confirmed the win the bot saw at {_t(at)} ({account}): is Givvy Wins "
                         f"running? Both windows stop there: the wins after it would be missing, and the trial would "
                         f"look worse for nothing.", "WARNING: ", "  ")
    if then:
        out += _wrap(_settings_text(then), "At the marker: ", "    ")
        was, now_on = then.get("accounts") or [], current.get("accounts") or []
        changed = [k for k in current if (set(was) != set(now_on) if k == "accounts" else then.get(k) != current[k])]
        out += _wrap("; ".join(f"{k} {_value(current[k])} (was {_value(then.get(k, '-'))})" for k in changed)
                     or "the same", "Now: ", "    ")
        if set(was) != set(now_on):
            out += _wrap(f"WARNING: the ticked accounts changed since the trial started ({', '.join(was) or 'none'} -> "
                         f"{', '.join(now_on) or 'none'}). The windows no longer compare like with like: press 'Start "
                         f"a new trial window now' in Settings > Results, or set [trial] start.", "", "  ")
        if then.get("hop_mode") != current.get("hop_mode"):
            out += _wrap(f"WARNING: the crowd hop's switch changed since the trial started ({then.get('hop_mode')} -> "
                         f"{current.get('hop_mode')}). Re-mark the window: 'Start a new trial window now' in Settings > "
                         f"Results, or set [trial] start.", "", "  ")
    if w["bad"]:
        out += _wrap("Unreadable in [trial], so ignored: " + "; ".join(w["bad"]), "", "  ")
    # NO_DATA is for the real 'different file' case, a baseline with nothing in it. An empty trial window is normal for
    # the first hour after the marker with the tracker set: telling John then that his setup is wrong would have him
    # changing paths.db and paths.givvy_wins_db when they are right.
    if not base[ALL].giveaways:
        out += [""] + _wrap(NO_DATA, "NOTE: ", "  ")
    elif w["trial"] is not None and w["trial"][1] <= w["trial"][0]:
        out += [""] + _wrap(_empty_trial(w), "NOTE: ", "  ")

    wb = base[ALL].pack_wins
    wt = trial[ALL].pack_wins if trial else 0
    cmp = compare(base[ALL], trial[ALL]) if trial else {}
    head = cmp.get("per_day")
    out.append("")
    if trial is None:
        out.append("VERDICT: no trial yet")
    else:
        v = verdict(wt, head)
        if v == "too early":
            detail = f"{wt} pack wins in the trial; a verdict needs at least {MIN_WINS}"
        elif head is None:
            detail = "The baseline has no pack wins"
        else:
            detail = f"Pack wins per account-day x{head.value:.2f} (95% range {head.low:.2f}-{head.high:.2f})"
        out.append(f"VERDICT: {v}. {detail}")
    for sentence in can_show(wb, wt):
        out += _wrap(sentence)
    out += _wrap(RULE)

    accounts = _order(base, trial)
    out += [""] + _grid("PER ACCOUNT", accounts, ROWS, base, trial, Numbers)
    out += _wrap("Rows before 9/23 22:01 have no titles, so their repeats are only partly found (a row within 180 s of "
                 "the last in that stream): the old version's rows per entry reads about 1.06 before then and about 1.2 "
                 "after. Only packs count as pack wins (giveaway_info.grade_title, the rule the bot enters by).", "  ",
                 "  ")

    if trial is not None:
        out += ["", "TRIAL / BEFORE, all accounts (95% range: pack wins overdispersed 1.35x)"]
        for key, text in (("per_day", "pack wins per account-day (the headline: outages and idle time count)"),
                          ("per_hour", "pack wins per active hour"),
                          ("time_of_day", "pack wins per active hour, time of day evened out")):
            r = cmp[key]
            out.append(f"  {text:<74}" + (f"x{r.value:.2f}  ({r.low:.2f}-{r.high:.2f})" if r else "-"))
        for key, text in (("expected_v", "expected wins per active hour, from viewers"),
                          ("expected_seen", "expected wins per active hour, from the count at entry")):
            out.append(f"  {text:<74}" + (f"x{cmp[key]:.2f}" if cmp[key] is not None else "-"))

    b, t = base[ALL], (trial[ALL] if trial else Numbers())
    keys = sorted(set(b.hours_by_accounts) | set(b.pack_by_accounts) | set(t.hours_by_accounts) |
                  set(t.pack_by_accounts))
    rows = [(f"  {k} account{'s' if k != 1 else ''} entering" if k else "  no account entering",
             b.hours_by_accounts.get(k, 0), b.pack_by_accounts.get(k, 0), t.hours_by_accounts.get(k, 0),
             t.pack_by_accounts.get(k, 0)) for k in keys]
    out += [""] + _split("HOURS BY HOW MANY OF OUR ACCOUNTS ENTERED IN THAT CLOCK HOUR (all accounts)", rows,
                         trial is not None)
    rows = [(f"  {band}", b.band_hours[i], b.band_pack[i], t.band_hours[i], t.band_pack[i])
            for i, band in enumerate(BANDS)]
    out += [""] + _split("TIME OF DAY, local (all accounts)", rows, trial is not None)

    stays = _order(base_stays, trial_stays)
    out += [""] + _grid("TRIAL-ONLY FACTS: from the stay log and the crowd hop's decisions, which only 1.2.0 writes "
                        "('-' = none)", stays, STAY_ROWS, base_stays, trial_stays, Stays,
                        show_before=base_stays[ALL].visits > 0,
                        show_after=trial_stays is not None and trial_stays[ALL].visits > 0)
    for name, s in (("before", base_stays[ALL] if base_stays[ALL].visits else None),
                    ("trial", trial_stays[ALL] if trial_stays else None)):
        if s is not None:
            out += _stay_lines(name, s)

    out += ["", "WHAT THIS TRIAL CAN AND CANNOT SHOW (John's database 9/20-9/24, measured 2026-09-24)"]
    for text in CAN_SHOW:
        out += _wrap(text, "- ")
    out += ["", "CONFOUNDS"]
    for text in CONFOUNDS:
        out += _wrap(text, "- ")
    return "\n".join(out)


def trial_report(store, cfg, now: float | None = None, accounts=None) -> str:
    """The whole report as plain text: Settings > Results and --trial-report show exactly this. `accounts`: the ones
    ticked now (the window passes its own; None = from ui_state.json and config.toml, for the command line)."""
    now = time.time() if now is None else now
    w = windows(store, cfg, now)
    base, base_stays = numbers(store, cfg, *w["baseline"]), trial_only(store, *w["baseline"])
    trial = trial_stays = None
    if w["trial"] is not None:
        trial, trial_stays = numbers(store, cfg, *w["trial"]), trial_only(store, *w["trial"])
    try:
        then = json.loads(store.kv_get("trial_settings") or "{}")
    except ValueError:
        then = {}
    latest = None
    if cfg.paths.givvy_wins_db:
        latest = store._x("SELECT at, account, seller, prize FROM win_history WHERE ref IS NOT NULL "
                          "ORDER BY at DESC LIMIT 1").fetchone()
    return format_trial(w, base, trial, base_stays, trial_stays, now=now, label=store.kv_get("trial_label") or "",
                        then=then if isinstance(then, dict) else {},
                        current=snapshot(cfg, ticked_accounts(cfg) if accounts is None else accounts),
                        latest=latest, tracker=bool(cfg.paths.givvy_wins_db), db=str(cfg.root / cfg.paths.db))
