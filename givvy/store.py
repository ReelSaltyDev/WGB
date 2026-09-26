"""SQLite persistence. One connection, autocommit, thread-safe via check_same_thread=False + lock."""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections import namedtuple
from pathlib import Path

from .models import GiveawayListing, LiveShow

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows(id TEXT PRIMARY KEY, seller TEXT, title TEXT, game TEXT, viewers INT,
  first_seen REAL, last_seen REAL, ended INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS giveaways(live_product_id TEXT PRIMARY KEY, show_id TEXT, seller TEXT, title TEXT,
  only_followers INT, quantity INT, started_at REAL, ended_at REAL, score REAL);
CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, live_product_id TEXT, show_id TEXT, seller TEXT,
  device TEXT, entered_at REAL, entries_seen INT, result TEXT);
CREATE TABLE IF NOT EXISTS follows(account TEXT NOT NULL DEFAULT 'main', seller TEXT NOT NULL, followed_at REAL,
  PRIMARY KEY(account, seller));
CREATE TABLE IF NOT EXISTS favorites(seller TEXT PRIMARY KEY, added_at REAL);
CREATE TABLE IF NOT EXISTS blacklist(seller TEXT PRIMARY KEY, reason TEXT, added_at REAL);
CREATE TABLE IF NOT EXISTS wins(id INTEGER PRIMARY KEY, live_product_id TEXT, show_id TEXT, seller TEXT,
  title TEXT, detected_at REAL);
CREATE TABLE IF NOT EXISTS activity(show_id TEXT, kind TEXT, value REAL, t REAL);
CREATE INDEX IF NOT EXISTS activity_t ON activity(t);
CREATE TABLE IF NOT EXISTS device_events(id INTEGER PRIMARY KEY, ts REAL, device TEXT, event TEXT);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, ts REAL, place TEXT, message TEXT, screenshot TEXT);
CREATE TABLE IF NOT EXISTS win_history(id INTEGER PRIMARY KEY, at REAL, account TEXT, seller TEXT, prize TEXT,
  source TEXT);
CREATE INDEX IF NOT EXISTS win_history_at ON win_history(at);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
-- 1.2.0 trial: one row per stay in a stream and per failed stream open, and one per crowd-hop decision.
-- 1.1.x does not know them: after a revert they are simply left as they are.
CREATE TABLE IF NOT EXISTS visits(id INTEGER PRIMARY KEY, account TEXT, seller TEXT, show_id TEXT, viewers INT,
  kind TEXT, first_entrants INT, joined_at REAL, seen_at REAL, left_at REAL, reason TEXT, opened INT DEFAULT 1);
CREATE INDEX IF NOT EXISTS visits_joined ON visits(joined_at);
CREATE TABLE IF NOT EXISTS hop_decisions(id INTEGER PRIMARY KEY, at REAL, account TEXT, mode TEXT, action TEXT,
  reason TEXT, trigger TEXT, show_id TEXT, seller TEXT, viewers INT, seen_median REAL, crowd REAL, per_hour REAL,
  share REAL, value REAL, best_show_id TEXT, best_seller TEXT, best_viewers INT, best_seen_median REAL,
  best_crowd REAL, best_per_hour REAL, best_share REAL, best_value REAL, best_age_s REAL, moves_last_hour INT);
CREATE INDEX IF NOT EXISTS hop_decisions_at ON hop_decisions(at);
CREATE INDEX IF NOT EXISTS hop_decisions_account ON hop_decisions(account, at);
"""


def same_prize(a: str, b: str) -> bool:
    """'Free Pack Givey #4' and 'Free Pack Givey #4 [Abyss Eye Korean Pack]' are the same prize: the bot adds the
    giveaway's description in brackets, the order history does not."""
    cut = lambda s: re.sub(r"\s*\[.*\]\s*$", "", s or "").strip().lower()
    return cut(a) == cut(b)


# One entries row, as Store.last_row returns it. key = live_product_id, the camper's '<show id>:<second>'.
EntryRow = namedtuple("EntryRow", "key at result entries_seen peak prize")


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate_pre_accounts()
        self.db.executescript(SCHEMA)
        self._migrate_entries()
        self.lock = threading.Lock()

    def _cols(self, table: str) -> list[str]:
        return [r[1] for r in self.db.execute(f"PRAGMA table_info({table})").fetchall()]

    def _migrate_pre_accounts(self):
        """Before multiple accounts, follows was keyed on seller alone. Everything
        recorded then belonged to the one login there was: 'main'."""
        cols = self._cols("follows")
        if cols and "account" not in cols:
            self.db.executescript("""
                ALTER TABLE follows RENAME TO follows_pre_accounts;
                CREATE TABLE follows(account TEXT NOT NULL DEFAULT 'main', seller TEXT NOT NULL, followed_at REAL,
                  PRIMARY KEY(account, seller));
                INSERT INTO follows(account, seller, followed_at) SELECT 'main', seller, followed_at FROM follows_pre_accounts;
                DROP TABLE follows_pre_accounts;
            """)

    def _migrate_entries(self):
        if "account" not in self._cols("entries"):
            self.db.execute("ALTER TABLE entries ADD COLUMN account TEXT NOT NULL DEFAULT 'main'")
        if "prize" not in self._cols("entries"):       # the card title: its '#12' is how misses are counted
            self.db.execute("ALTER TABLE entries ADD COLUMN prize TEXT")
        if "account" not in self._cols("wins"):        # a win used to be recorded without saying whose it was
            self.db.execute("ALTER TABLE wins ADD COLUMN account TEXT")
        if "ref" not in self._cols("win_history"):     # the tracker's order number, once a win is confirmed there
            self.db.execute("ALTER TABLE win_history ADD COLUMN ref TEXT")
        # 1.2.0: the giveaway's highest entrant count, and how our entry was proven (direct / tick / late / redo).
        # NULL on rows written before, and by 1.1.1 after a revert (every 1.1.1 query names its columns).
        if "peak_entries" not in self._cols("entries"):
            self.db.execute("ALTER TABLE entries ADD COLUMN peak_entries INT")
        if "confirmed_by" not in self._cols("entries"):
            self.db.execute("ALTER TABLE entries ADD COLUMN confirmed_by TEXT")
        # For last_row(), which runs on every new card and every flicker. Here and not in SCHEMA: on a database from
        # before accounts, the account column only exists after the ALTER above.
        self.db.execute("CREATE INDEX IF NOT EXISTS entries_account_show ON entries(account, show_id, entered_at)")

    def _x(self, sql: str, args: tuple = ()):
        with self.lock:
            return self.db.execute(sql, args)

    # shows
    def upsert_show(self, s: LiveShow, now: float | None = None):
        now = now or time.time()
        self._x("INSERT INTO shows(id,seller,title,game,viewers,first_seen,last_seen) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET viewers=excluded.viewers, last_seen=excluded.last_seen, ended=0",
                (s.id, s.seller, s.title, s.game, s.viewers, now, now))

    def mark_ended_except(self, live_ids: set[str]):
        with self.lock:
            rows = self.db.execute("SELECT id FROM shows WHERE ended=0").fetchall()
            for (sid,) in rows:
                if sid not in live_ids:
                    self.db.execute("UPDATE shows SET ended=1 WHERE id=?", (sid,))

    # giveaways
    def record_giveaway(self, live_product_id: str, show: LiveShow, listing: GiveawayListing | None,
                        started_at: float, score: float | None = None):
        self._x("INSERT OR IGNORE INTO giveaways(live_product_id,show_id,seller,title,only_followers,quantity,started_at,score) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (live_product_id, show.id, show.seller, listing.title if listing else "",
                 int(listing.only_followers) if listing else None, listing.quantity if listing else None, started_at, score))

    def end_giveaway(self, live_product_id: str, ended_at: float | None = None):
        self._x("UPDATE giveaways SET ended_at=? WHERE live_product_id=?", (ended_at or time.time(), live_product_id))

    # entries
    def record_entry(self, live_product_id: str, show: LiveShow, device: str, entries_seen: int | None,
                     result: str, at: float | None = None, account: str = "main", prize: str = "",
                     confirmed_by: str | None = None):
        self._x("INSERT INTO entries(live_product_id,show_id,seller,device,entered_at,entries_seen,result,account,prize,"
                "confirmed_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (live_product_id, show.id, show.seller, device, at or time.time(), entries_seen, result, account,
                 prize or None, confirmed_by))

    # what the bot learns about streams (givvy.chooser.ActivityBook), so a restart does not forget it
    def record_activity(self, kind: str, show_id: str, value: float, t: float) -> None:
        self._x("INSERT INTO activity(show_id,kind,value,t) VALUES(?,?,?,?)", (show_id, kind, value, t))

    def load_activity(self, since: float) -> list[tuple]:
        """(kind, show_id, value, t), oldest first."""
        return self._x("SELECT kind, show_id, value, t FROM activity WHERE t>=? ORDER BY t", (since,)).fetchall()

    def prune_activity(self, older_than: float) -> None:
        self._x("DELETE FROM activity WHERE t<?", (older_than,))

    def confirm_entry(self, live_product_id: str, prize: str = "", by: str = "late") -> None:
        """An 'unverified' tap was later seen to have worked: by 'late' ("You're in" on a later read) or 'tick' (the
        badge's icon)."""
        self._x("UPDATE entries SET result='entered', prize=COALESCE(prize, ?), confirmed_by=? "
                "WHERE live_product_id=? AND result='unverified'", (prize or None, by, live_product_id))

    def drop_unverified(self, live_product_id: str) -> None:
        """An 'unverified' tap that turned out never to have landed."""
        self._x("DELETE FROM entries WHERE live_product_id=? AND result='unverified'", (live_product_id,))

    def already_tried(self, live_product_id: str) -> bool:
        return self._x("SELECT 1 FROM entries WHERE live_product_id=? LIMIT 1", (live_product_id,)).fetchone() is not None

    def last_row(self, account: str, show_id: str, since: float) -> EntryRow | None:
        """This account's newest row in this stream written at or after `since`. Read from the database rather than
        from memory so it still works across a restart: 3 of the 21 repeat rows counted 9/23-24 came either side of
        an emulator restart (a new stay), and rejoining the same stream after an app restart is to become normal."""
        row = self._x("SELECT live_product_id, entered_at, result, entries_seen, peak_entries, prize FROM entries "
                      "WHERE account=? AND show_id=? AND entered_at>=? ORDER BY entered_at DESC LIMIT 1",
                      (account, show_id, since)).fetchone()
        return EntryRow(*row) if row else None

    def note_peak(self, live_product_id: str, peak: int) -> None:
        """The highest entrant count seen on this giveaway. Kept as the MAX: one giveaway split into several turns by
        the badge flickering adds up to one peak, never below the count at entry."""
        self._x("UPDATE entries SET peak_entries = MAX(COALESCE(peak_entries, 0), COALESCE(entries_seen, 0), ?) "
                "WHERE live_product_id=?", (int(peak), live_product_id))

    def fill_prize(self, live_product_id: str, prize: str) -> None:
        """A row written without a title (a fast tap proved by the tick) takes the numbered title the next time the
        card is read: the report counts giveaways and gaps by that number."""
        if prize:
            self._x("UPDATE entries SET prize=? WHERE live_product_id=? AND prize IS NULL", (prize, live_product_id))

    def entries_in_last_hour(self, now: float | None = None, account: str | None = None) -> int:
        """The cap is per account: one login hitting its limit must not stop another."""
        now = now or time.time()
        sql = "SELECT COUNT(*) FROM entries WHERE result IN ('entered','unverified') AND entered_at>?"
        args: tuple = (now - 3600,)
        if account is not None:
            sql, args = sql + " AND account=?", args + (account,)
        return self._x(sql, args).fetchone()[0]

    # follows / wins / events / errors
    def record_follow(self, seller: str, account: str = "main"):
        self._x("INSERT OR IGNORE INTO follows(account,seller,followed_at) VALUES(?,?,?)",
                (account, seller.lower(), time.time()))

    def followed_sellers(self, account: str | None = None) -> list[str]:
        """Sellers one account follows, or (no account) anyone of ours follows."""
        if account is None:
            return [r[0] for r in self._x("SELECT DISTINCT seller FROM follows ORDER BY seller").fetchall()]
        return [r[0] for r in self._x("SELECT seller FROM follows WHERE account=? ORDER BY seller",
                                      (account,)).fetchall()]

    def rename_account(self, old: str, new: str):
        """History follows the account when it is renamed in the window: entries, follows, its wins (below), and what
        was learned about it (its Whatnot name, givvy/whoami.py)."""
        self._x("UPDATE entries SET account=? WHERE account=?", (new, old))
        self._x("UPDATE wins SET account=? WHERE account=?", (new, old))
        self._x("UPDATE OR IGNORE follows SET account=? WHERE account=?", (new, old))
        self._x("DELETE FROM follows WHERE account=?", (old,))
        # The trial report counts stays and moves per account, and the crowd hop's hourly cap reads its own moves.
        self._x("UPDATE visits SET account=? WHERE account=?", (new, old))
        self._x("UPDATE hop_decisions SET account=? WHERE account=?", (new, old))
        # ...and its wins: left behind, the report showed the old name with all the wins and no hours, and the new one
        # with hours and only the wins since (the tracker's own rows are 'known' and never re-imported). The Win Charts
        # label, the saved stream and a safety pause in progress go with it too.
        self._x("UPDATE win_history SET account=? WHERE account=?", (new, old))
        for kind in ("label:", "camped:", "breaker:", "whatnot_name:", "joins_seen:"):
            self._x("UPDATE OR REPLACE kv SET key=? WHERE key=?", (kind + new, kind + old))

    def favorite_add(self, seller: str):
        self._x("INSERT OR IGNORE INTO favorites(seller, added_at) VALUES(?,?)", (seller.strip().lower(), time.time()))

    def favorite_remove(self, seller: str) -> bool:
        seller = seller.strip().lower()
        had = seller in self.favorites()
        self._x("DELETE FROM favorites WHERE seller=?", (seller,))
        return had

    def favorites(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT seller FROM favorites ORDER BY seller").fetchall()]

    def blacklist_add(self, seller: str, reason: str = ""):
        self._x("INSERT OR IGNORE INTO blacklist(seller, reason, added_at) VALUES(?,?,?)",
                (seller, reason, time.time()))

    def blacklist_remove(self, seller: str) -> bool:
        had = seller in self.blacklisted_sellers()
        self._x("DELETE FROM blacklist WHERE seller=?", (seller,))
        return had

    def blacklisted_sellers(self) -> set[str]:
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT seller FROM blacklist").fetchall()}

    def blacklist_rows(self) -> list[tuple]:
        with self.lock:
            return self.db.execute("SELECT seller, reason, added_at FROM blacklist ORDER BY added_at").fetchall()

    def record_win(self, live_product_id: str, show: LiveShow, title: str, account: str = ""):
        now = time.time()
        self._x("INSERT INTO wins(live_product_id,show_id,seller,title,detected_at,account) VALUES(?,?,?,?,?,?)",
                (live_product_id, show.id, show.seller, title, now, account or None))
        self.add_win(now, account, show.seller, title, source="live")

    # The win history behind Win Charts: one row per win. The wins table above has hundreds of repeats from the
    # days when one win raised twenty alerts, and no account before 2026-09-23, so the charts do not use it.
    WIN_REPEAT_S = 900          # the same prize again within 15 minutes: the same win
    WIN_SAME_DIALOG_S = 90      # anything in the same stream within 90 s: the winner dialog seen twice

    def add_win(self, at: float, account: str, seller: str, prize: str = "", source: str = "live") -> bool:
        """False when it is the same win again: same account and stream, and either within 90 seconds (the dialog
        seen twice, measured 19 s apart) or the same prize within 15 minutes. Two wins in one stream a few minutes
        apart are two wins, even when the first was seen straight after joining and has no prize name: measured
        2026-09-24, a 15-minute rule that let an unnamed prize match anything merged 3 real back-to-back wins."""
        account, prize = account or "", (prize or "").strip()
        with self.lock:
            near = self.db.execute("SELECT id, prize, at FROM win_history WHERE account=? AND lower(seller)=lower(?) "
                                   "AND at BETWEEN ? AND ?", (account, seller, at - self.WIN_REPEAT_S,
                                                              at + self.WIN_REPEAT_S)).fetchall()
            for wid, had, when in near:
                if abs(when - at) <= self.WIN_SAME_DIALOG_S and (not had or not prize or same_prize(had, prize)):
                    if prize and not had:
                        self.db.execute("UPDATE win_history SET prize=? WHERE id=?", (prize, wid))
                    return False
                if prize and had and same_prize(had, prize):
                    return False
            self.db.execute("INSERT INTO win_history(at,account,seller,prize,source) VALUES(?,?,?,?,?)",
                            (at, account, seller, prize, source))
            return True

    TRACKER_MATCH_S = 1200     # the order's time and the moment the bot saw the winner: measured within 20 minutes

    def add_tracker_win(self, ref: str, at: float, account: str, seller: str, prize: str) -> str:
        """One win from the Givvy Wins tracker. 'known' if it is already in; 'matched' if the bot had seen it (that
        row takes the tracker's time and title); 'added' if the bot missed it."""
        with self.lock:
            if self.db.execute("SELECT 1 FROM win_history WHERE ref=?", (ref,)).fetchone():
                return "known"
            row = self.db.execute(
                "SELECT id FROM win_history WHERE ref IS NULL AND account=? AND lower(seller)=lower(?) "
                "AND at BETWEEN ? AND ? ORDER BY abs(at - ?) LIMIT 1",
                (account, seller, at - self.TRACKER_MATCH_S, at + self.TRACKER_MATCH_S, at)).fetchone()
            if row:
                self.db.execute("UPDATE win_history SET at=?, prize=?, ref=?, source='tracker' WHERE id=?",
                                (at, prize, ref, row[0]))
                return "matched"
            self.db.execute("INSERT INTO win_history(at,account,seller,prize,source,ref) VALUES(?,?,?,?,?,?)",
                            (at, account, seller, prize, "tracker", ref))
            return "added"

    def win_sources(self) -> dict[str, int]:
        with self.lock:
            return dict(self.db.execute("SELECT source, COUNT(*) FROM win_history GROUP BY source").fetchall())

    def win_history(self, since: float = 0.0) -> list[tuple]:
        """(at, account, seller, prize), oldest first."""
        with self.lock:
            return self.db.execute("SELECT at, account, seller, prize FROM win_history WHERE at>=? ORDER BY at",
                                   (since,)).fetchall()

    def latest_win(self) -> tuple[str, str] | None:
        """(account, prize) of the win added last."""
        with self.lock:
            return self.db.execute("SELECT account, prize FROM win_history ORDER BY id DESC LIMIT 1").fetchone()

    def wins_since(self, since: float) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM win_history WHERE at>=?", (since,)).fetchone()[0]

    def win_count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM win_history").fetchone()[0]

    def kv_get(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str):
        self._x("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)", (key, value))

    # Stays in streams, for the 1.2.0 trial report (the reasons a stay ends are listed on givvy.camper.Visit). The
    # activity table cannot answer 'how long in 70+ viewer streams' or 'joins per hour': it keeps 24 h and no account.
    def start_visit(self, account: str, show: LiveShow, at: float, kind: str = "", opened: bool = True) -> int:
        """A stay begins; returns its row id. `opened`: a stream link was fired for it (False = back from a pause)."""
        return self._x("INSERT INTO visits(account,seller,show_id,viewers,kind,joined_at,seen_at,opened) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (account, show.seller, show.id, show.viewers, kind, at, at, int(opened))).lastrowid

    def visit_seen(self, visit_id: int, at: float) -> None:
        """Still there: a stay cut off by a crash ends here (close_open_visits)."""
        self._x("UPDATE visits SET seen_at=? WHERE id=?", (at, visit_id))

    def visit_first_entrants(self, visit_id: int, n: int) -> None:
        """The first entrant count read on a card in this stay. Never replaced by a later one."""
        self._x("UPDATE visits SET first_entrants=? WHERE id=? AND first_entrants IS NULL", (int(n), visit_id))

    def end_visit(self, visit_id: int, at: float, reason: str) -> None:
        self._x("UPDATE visits SET left_at=?, seen_at=?, reason=? WHERE id=?", (at, at, reason, visit_id))

    def record_open_attempt(self, account: str, show: LiveShow, at: float, reason: str) -> None:
        """A stream link that did not become a stay: 'open_failed' (it did not land) or 'blacklisted' (tariffs on
        arrival). A zero-length row: it counts as a join and a failed open, never as time in a stream."""
        self._x("INSERT INTO visits(account,seller,show_id,viewers,joined_at,seen_at,left_at,reason,opened) "
                "VALUES(?,?,?,?,?,?,?,?,1)", (account, show.seller, show.id, show.viewers, at, at, at, reason))

    def close_open_visits(self, reason: str = "app stopped") -> int:
        """Stays a crash or kill left open end at the last time the account was seen there. Returns how many."""
        return self._x("UPDATE visits SET left_at=COALESCE(seen_at, joined_at), reason=? WHERE left_at IS NULL",
                       (reason,)).rowcount

    def visits(self, since: float, until: float) -> list[tuple]:
        """(account, seller, show_id, viewers, kind, first_entrants, joined_at, left_at, reason, opened) for the stays
        overlapping [since, until), oldest first. A stay still open ends at its last seen_at."""
        with self.lock:
            return self.db.execute(
                "SELECT account, seller, show_id, viewers, kind, first_entrants, joined_at, "
                "COALESCE(left_at, seen_at, joined_at), reason, opened FROM visits "
                "WHERE joined_at<? AND COALESCE(left_at, seen_at, joined_at)>=? ORDER BY joined_at, id",
                (until, since)).fetchall()

    # Crowd-hop decisions: the stream we are in and the best free candidate, on one scale (crowd = expected final
    # entrants, per_hour = giveaways/h, share = pack share, value = expected pack wins/h).
    HOP_SIDE = ("show_id", "seller", "viewers", "seen_median", "crowd", "per_hour", "share", "value")

    def record_hop(self, at: float, account: str, mode: str, action: str, reason: str, trigger: str,
                   cur: dict | None = None, best: dict | None = None, moves: int = 0) -> None:
        """One decision. action: stayed / would_leave / left / cancelled / upgrade; trigger: socket / screen / timeout
        / check. `cur` and `best` take the HOP_SIDE keys, and best 'age_s' (how old its giveaway is); a missing key
        is NULL. `moves`: voluntary moves in the hour before."""
        cur, best = cur or {}, best or {}
        cols = ("at", "account", "mode", "action", "reason", "trigger", *self.HOP_SIDE,
                *(f"best_{k}" for k in self.HOP_SIDE), "best_age_s", "moves_last_hour")
        vals = (at, account, mode, action, reason, trigger, *(cur.get(k) for k in self.HOP_SIDE),
                *(best.get(k) for k in self.HOP_SIDE), best.get("age_s"), int(moves))
        self._x(f"INSERT INTO hop_decisions({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)

    def voluntary_moves(self, account: str, since: float) -> int:
        """Moves this account chose after `since`: 'left' (the crowd hop) and 'upgrade'. From the database, so the
        hourly cap survives a restart (59 app restarts in 7.3 days to 9/24)."""
        return self._x("SELECT COUNT(*) FROM hop_decisions WHERE account=? AND action IN ('left', 'upgrade') AND at>?",
                       (account, since)).fetchone()[0]

    def hop_rows(self, since: float, until: float | None = None) -> list[dict]:
        """Decisions from `since` (up to `until`), oldest first, as column -> value."""
        sql, args = "SELECT * FROM hop_decisions WHERE at>=?", (since,)
        if until is not None:
            sql, args = sql + " AND at<?", args + (until,)
        with self.lock:
            cur = self.db.execute(sql + " ORDER BY at, id", args)
            names = [d[0] for d in cur.description]
            return [{k: v for k, v in zip(names, row) if k != "id"} for row in cur.fetchall()]

    def record_device_event(self, device: str, event: str):
        self._x("INSERT INTO device_events(ts,device,event) VALUES(?,?,?)", (time.time(), device, event))

    def record_error(self, place: str, message: str, screenshot: str | None = None):
        self._x("INSERT INTO errors(ts,place,message,screenshot) VALUES(?,?,?,?)", (time.time(), place, message, screenshot))

    def status_summary(self, hours: float = 24) -> dict:
        """Wins come from win_history: the legacy wins table holds hundreds of repeats (633 rows on 2026-09-24, 622
        of them with no account). 'missed' used to be every row not entered, skipped and unverified included."""
        since = time.time() - hours * 3600
        q = lambda sql: self._x(sql, (since,)).fetchone()[0]
        return {
            "entries": q("SELECT COUNT(*) FROM entries WHERE result='entered' AND entered_at>?"),
            "missed": q("SELECT COUNT(*) FROM entries WHERE result='missed' AND entered_at>?"),
            "giveaways_seen": q("SELECT COUNT(*) FROM giveaways WHERE started_at>?"),
            "wins": q("SELECT COUNT(*) FROM win_history WHERE at>?"),
            "follows": q("SELECT COUNT(*) FROM follows WHERE followed_at>?"),
            "errors": q("SELECT COUNT(*) FROM errors WHERE ts>?"),
        }
