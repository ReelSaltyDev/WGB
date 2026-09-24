"""SQLite persistence. One connection, autocommit, thread-safe via check_same_thread=False + lock."""
from __future__ import annotations

import re
import sqlite3
import threading
import time
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
"""


def same_prize(a: str, b: str) -> bool:
    """'Free Pack Givey #4' and 'Free Pack Givey #4 [Abyss Eye Korean Pack]' are the same prize: the bot adds the
    giveaway's description in brackets, the order history does not."""
    cut = lambda s: re.sub(r"\s*\[.*\]\s*$", "", s or "").strip().lower()
    return cut(a) == cut(b)


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
                     result: str, at: float | None = None, account: str = "main", prize: str = ""):
        self._x("INSERT INTO entries(live_product_id,show_id,seller,device,entered_at,entries_seen,result,account,prize) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (live_product_id, show.id, show.seller, device, at or time.time(), entries_seen, result, account,
                 prize or None))

    # what the bot learns about streams (givvy.chooser.ActivityBook), so a restart does not forget it
    def record_activity(self, kind: str, show_id: str, value: float, t: float) -> None:
        self._x("INSERT INTO activity(show_id,kind,value,t) VALUES(?,?,?,?)", (show_id, kind, value, t))

    def load_activity(self, since: float) -> list[tuple]:
        """(kind, show_id, value, t), oldest first."""
        return self._x("SELECT kind, show_id, value, t FROM activity WHERE t>=? ORDER BY t", (since,)).fetchall()

    def prune_activity(self, older_than: float) -> None:
        self._x("DELETE FROM activity WHERE t<?", (older_than,))

    def confirm_entry(self, live_product_id: str, prize: str = "") -> None:
        """An 'unverified' tap was later seen to have worked."""
        self._x("UPDATE entries SET result='entered', prize=COALESCE(prize, ?) WHERE live_product_id=? AND result='unverified'",
                (prize or None, live_product_id))

    def drop_unverified(self, live_product_id: str) -> None:
        """An 'unverified' tap that turned out never to have landed."""
        self._x("DELETE FROM entries WHERE live_product_id=? AND result='unverified'", (live_product_id,))

    def already_tried(self, live_product_id: str) -> bool:
        return self._x("SELECT 1 FROM entries WHERE live_product_id=? LIMIT 1", (live_product_id,)).fetchone() is not None

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
        """History follows the account when it is renamed in the window."""
        self._x("UPDATE entries SET account=? WHERE account=?", (new, old))
        self._x("UPDATE OR IGNORE follows SET account=? WHERE account=?", (new, old))
        self._x("DELETE FROM follows WHERE account=?", (old,))

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

    def record_device_event(self, device: str, event: str):
        self._x("INSERT INTO device_events(ts,device,event) VALUES(?,?,?)", (time.time(), device, event))

    def record_error(self, place: str, message: str, screenshot: str | None = None):
        self._x("INSERT INTO errors(ts,place,message,screenshot) VALUES(?,?,?,?)", (time.time(), place, message, screenshot))

    def status_summary(self, hours: float = 24) -> dict:
        since = time.time() - hours * 3600
        q = lambda sql: self._x(sql, (since,)).fetchone()[0]
        return {
            "entries": q("SELECT COUNT(*) FROM entries WHERE result='entered' AND entered_at>?"),
            "missed": q("SELECT COUNT(*) FROM entries WHERE result!='entered' AND entered_at>?"),
            "giveaways_seen": q("SELECT COUNT(*) FROM giveaways WHERE started_at>?"),
            "wins": q("SELECT COUNT(*) FROM wins WHERE detected_at>?"),
            "follows": q("SELECT COUNT(*) FROM follows WHERE followed_at>?"),
            "errors": q("SELECT COUNT(*) FROM errors WHERE ts>?"),
        }
