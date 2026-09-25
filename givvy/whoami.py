"""Which Whatnot username is this account? Learned from the stream chat, so wins are recognised even when the account's
label in the window is not exactly its Whatnot name.

A win is only seen as '<username> won the giveaway!' on screen, matched against the account's names (its label, its
device name and whatnot_username). A friend's install counted no wins at all (2026-09-24): with no Givvy Wins tracker,
that match is the only way a win reaches the counter, the charts and the celebration, and it fails silently whenever
the label differs from the real Whatnot name. Whatnot's chat shows '<username> joined ..' for everyone who comes in,
our account included (seen on emulator 3: 'saltygoblin joined', whose label was then reelsalty85). Strangers join at
the same moments, so one sighting proves nothing; the name that shows up right after OUR joins in at least NEED
different streams, and in most of the joins where any name was read, is ours.

The learned name only adds a name to match wins against. Counting, charts and the celebration caption stay on the
account label given in the window."""
from __future__ import annotations

import json
from collections import Counter

WINDOW_S = 15          # a join shows in the chat within seconds of the stream opening
NEED = 3               # distinct streams the same name must be seen in after our join
SHARE = 0.8            # ...and in this share of the joins where any name was read
KEEP = 12              # joins remembered


def learned(joins: list[list]) -> str | None:
    """joins: [show_id, [names seen right after our join]] per join, oldest first."""
    with_names = [(sid, set(names)) for sid, names in joins if names]
    if not with_names:
        return None
    streams: dict[str, set[str]] = {}
    count: Counter = Counter()
    for sid, names in with_names:
        for n in names:
            count[n] += 1
            streams.setdefault(n, set()).add(sid)
    good = [n for n, c in count.items() if len(streams[n]) >= NEED and c >= SHARE * len(with_names)]
    return good[0] if len(good) == 1 else None


class NameLearner:
    """Per account. `store` needs kv_get / kv_set."""

    def __init__(self, store, account: str):
        self.store, self.account = store, account
        self.show_id, self.until = "", 0.0
        self.names: set[str] = set()

    @property
    def key(self) -> str:
        return f"joins_seen:{self.account}"

    def name(self) -> str | None:
        """The learned Whatnot username, if there is one."""
        return self.store.kv_get(f"whatnot_name:{self.account}")

    def joined(self, show_id: str, now: float) -> None:
        """Our account has just opened this stream: watch the chat for WINDOW_S."""
        self._close()
        self.show_id, self.until, self.names = show_id, now + WINDOW_S, set()

    def saw(self, show_id: str, names: set[str], now: float) -> str | None:
        """Names read in the chat. Returns a NEWLY learned name (once), else None."""
        if show_id != self.show_id:
            return None
        if now > self.until:
            return self._close()
        self.names |= names
        return None

    def _close(self) -> str | None:
        if not self.show_id:
            return None
        sid, names = self.show_id, sorted(self.names)
        self.show_id, self.until, self.names = "", 0.0, set()
        try:
            joins = json.loads(self.store.kv_get(self.key) or "[]")
        except ValueError:
            joins = []
        joins = (joins + [[sid, names]])[-KEEP:]
        self.store.kv_set(self.key, json.dumps(joins))
        name = learned(joins)
        if name and name != self.name():
            self.store.kv_set(f"whatnot_name:{self.account}", name)
            return name
        return None
