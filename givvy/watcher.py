"""Phoenix live-socket watcher: joins chat:<show_id> topics and turns
livestream_update.activeGiveawayLiveProductId transitions into GiveawayEvents.

Measured 2026-09-15: an anonymous socket never receives giveaway_started /
giveaway_entry_count_updated / giveaway_won, and the prize name cannot be
recovered from the web API (activeGiveawayLiveProductId is a different id
namespace from shop listing ids, and the Upcoming Giveaways list does not change
when a giveaway starts). So events carry listing=None and the device reads the
prize and entry count on arrival. This module makes no HTTP calls, which also
keeps the socket heartbeat on time.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from .models import GAME_ORDER, GiveawayEvent, LiveShow

log = logging.getLogger(__name__)

HEARTBEAT_S = 20


@dataclass
class Frame:
    join_ref: str | None
    ref: str | None
    topic: str
    event: str
    payload: dict

    @property
    def show_id(self) -> str:
        return self.topic.split(":", 1)[1] if ":" in self.topic else self.topic


def parse_frame(raw: str) -> Frame:
    arr = json.loads(raw)
    return Frame(arr[0], arr[1], arr[2], arr[3], arr[4] if isinstance(arr[4], dict) else {})


def join_frame(ref: int, show_id: str) -> str:
    return json.dumps([str(ref), str(ref), f"chat:{show_id}", "phx_join",
                       {"adCampaignId": None, "livestreamSessionId": str(uuid.uuid4()), "locale": "en-US", "slo_stories": []}])


def leave_frame(join_ref: str, ref: int, show_id: str) -> str:
    return json.dumps([join_ref, str(ref), f"chat:{show_id}", "phx_leave", {}])


def heartbeat_frame(ref: int) -> str:
    return json.dumps([None, str(ref), "phoenix", "heartbeat", {}])


@dataclass
class ShowState:
    show: LiveShow
    active_event: GiveawayEvent | None = None
    join_ref: str | None = None
    viewers: int = 0
    seen_any_update: bool = False     # the first frame tells us the CURRENT state,
                                      # not that a giveaway just started

    def apply_update(self, payload: dict, now: float, emit: Callable[[GiveawayEvent], None]):
        """Turn one livestream_update into at most one end + one start event.

        `listing` is always None: the prize name is not obtainable from the web
        API (see the module docstring). The device reads it on arrival.
        """
        viewers = payload.get("activeViewers")
        if viewers is not None:
            self.viewers = int(viewers)
        new_id = payload.get("activeGiveawayLiveProductId") or None
        first = not self.seen_any_update
        self.seen_any_update = True
        cur = self.active_event.live_product_id if self.active_event else None
        if new_id == cur:
            return
        if self.active_event is not None:
            ended = dataclasses.replace(self.active_event, status="ended")
            self.active_event = None
            emit(ended)
        if new_id is None:
            return
        self.active_event = GiveawayEvent(show=self.show, live_product_id=new_id, status="active",
                                          listing=None, started_at=now,
                                          viewers=self.viewers or self.show.viewers,
                                          saw_start=not first)
        emit(self.active_event)


class Watcher:
    def __init__(self, client, client_version: str, slots: int, on_event: Callable[[GiveawayEvent], None]):
        self.client = client
        self.client_version = client_version
        self.slots = slots
        self.on_event = on_event
        self.states: dict[str, ShowState] = {}
        self._desired: list[LiveShow] = []
        self._followed: set[str] = set()
        self._ref = 0
        self._ws = None
        self._dirty = False          # set by set_shows(); the run loop reconciles joins/leaves

    # ---- planning (pure) ----
    def plan(self, shows: list[LiveShow], followed: set[str]) -> tuple[list[str], list[str]]:
        must = [s for s in shows if s.seller in followed]
        rest = sorted((s for s in shows if s.seller not in followed),
                      key=lambda s: (GAME_ORDER.get(s.game, 0), s.viewers), reverse=True)
        desired = (must + rest)[: self.slots]
        desired_ids = [s.id for s in desired]
        joins = [i for i in desired_ids if i not in self.states]
        leaves = [i for i in self.states if i not in desired_ids]
        self._desired = desired
        return joins, leaves

    def set_shows(self, shows: list[LiveShow], followed: set[str]):
        self._followed = set(followed)
        self._desired = shows
        self._dirty = True

    def active_events(self) -> list[GiveawayEvent]:
        return [st.active_event for st in self.states.values() if st.active_event is not None]

    # ---- socket loop ----
    async def run(self):
        import websockets
        from .whatnot_api import UA, socket_url
        backoff = 2
        while True:
            try:
                url = await asyncio.to_thread(socket_url, self.client_version)
                async with websockets.connect(url, additional_headers={"User-Agent": UA, "Origin": "https://www.whatnot.com"},
                                              max_size=2 ** 22, ping_interval=None) as ws:
                    self._ws = ws
                    backoff = 2
                    self.states = {}          # fresh socket: re-join everything
                    self._dirty = True
                    last_hb = time.time()
                    while True:
                        if self._dirty:
                            self._dirty = False
                            await self._reconcile()
                        if time.time() - last_hb > HEARTBEAT_S:
                            self._ref += 1
                            await ws.send(heartbeat_frame(self._ref))
                            last_hb = time.time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        await self._handle(parse_frame(raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("socket error: %s; reconnecting in %ss", e, backoff)
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _reconcile(self):
        joins, leaves = self.plan(self._desired, self._followed)
        for sid in leaves:
            st = self.states.pop(sid)
            if st.active_event is not None:
                self.on_event(dataclasses.replace(st.active_event, status="ended"))
            self._ref += 1
            await self._ws.send(leave_frame(st.join_ref or str(self._ref), self._ref, sid))
        by_id = {s.id: s for s in self._desired}
        for sid in joins:
            self._ref += 1
            self.states[sid] = ShowState(by_id[sid], join_ref=str(self._ref))
            await self._ws.send(join_frame(self._ref, sid))
            await asyncio.sleep(0.2)

    async def _handle(self, fr: Frame):
        if fr.event == "phx_reply" and fr.payload.get("status") == "error":
            log.warning("join error on %s: %s", fr.topic, fr.payload)
            self.states.pop(fr.show_id, None)
            return
        if fr.event != "livestream_update":
            return
        st = self.states.get(fr.show_id)
        if st is None:
            return
        if fr.payload.get("status") not in (None, "PLAYING"):
            if st.active_event is not None:
                self.on_event(dataclasses.replace(st.active_event, status="ended"))
                st.active_event = None
            return
        st.apply_update(fr.payload, now=time.time(), emit=self.on_event)
