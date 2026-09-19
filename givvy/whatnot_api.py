"""Whatnot web GraphQL + live-socket session, anonymous. Verified 2026-09-15."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable

from .models import GiveawayListing

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
GRAPHQL = "https://www.whatnot.com/services/graphql/?operationName={op}&ssr=0"
SOCKET_SESSION = "https://www.whatnot.com/services/live/socket/session"
SOCKET_URL = ("wss://www.whatnot.com/services/live/socket/websocket?_csrf_token={csrf}"
              "&client_layer=nextjs&client_type=web&client_version={ver}&token=&vsn=2.0.0")

Q_TAG_FEED_ID = (
    "query GetLivestreamTagFeedId($name:String!){getLivestreamTagByName(name:$name){id label feed{id}}}")

Q_GET_FEED = (
    "query GetFeed($feedId:ID!,$objectSize:Int,$objectCursor:String,$filters:[FilterInput],$sort:SortInput)"
    "{feed:getFeedFromOnboardingOption(id:$feedId,filters:$filters,sort:$sort){id objects(first:$objectSize,after:$objectCursor)"
    "{totalCount pageInfo{endCursor hasNextPage} edges{node{__typename ... on FeedEntity{object{__typename ... on LiveStream"
    "{id shippingSourceCountryCode activeViewers startTime status title livestreamCategories{id label deeplink} tags{id label} user{id username}}}}}}}}}")

Q_USER_LIVE = (
    "query GetUserLiveStreams($username:String!){getUser(username:$username){id username isLive "
    "livestreams(first:5){edges{node{id shippingSourceCountryCode status title activeViewers startTime}}}}}")

Q_LIVE_SHOP = (
    "query LiveShopFeed($liveId:ID!,$first:Int){liveShop(liveId:$liveId){feed(query:\"\",filters:null,sort:null,sessionId:null)"
    "{id objects(first:$first,after:null){edges{node{__typename ... on Section{id title sectionType contents{edges{node{__typename "
    "... on ListingNode{id title quantity transactionType listingStatus:publicStatus "
    "transactionProps{giveaway{onlyFollowers onlyDomestic buyerAppreciation}} salesChannels{id meta{id type}}}}}}}}}}}}}")

Transport = Callable[[str, str, dict], dict]


class WhatnotError(RuntimeError):
    pass


def http_transport(op: str, query: str, variables: dict, *, retries: int = 4) -> dict:
    body = json.dumps({"operationName": op, "variables": variables, "query": query}).encode()
    req = urllib.request.Request(
        GRAPHQL.format(op=op), data=body, method="POST",
        headers={"User-Agent": UA, "Content-Type": "application/json", "x-whatnot-app": "whatnot-web",
                 "x-whatnot-app-context": "next-js/browser", "x-whatnot-urs": "ANONYMOUS",
                 "accept-language": "en-US", "Origin": "https://www.whatnot.com"})
    delay = 2.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            if data.get("errors") and not data.get("data"):
                raise WhatnotError(f"{op}: {data['errors'][0].get('message')}")
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                log.warning("%s HTTP %s, retrying in %.0fs", op, e.code, delay)
                time.sleep(delay)
                delay = min(delay * 2, 600)
                continue
            raise WhatnotError(f"{op}: HTTP {e.code} {e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 600)
                continue
            raise WhatnotError(f"{op}: {e}") from e
    raise WhatnotError(f"{op}: retries exhausted")


def socket_csrf() -> str:
    req = urllib.request.Request(SOCKET_SESSION, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["csrf_token"]


def socket_url(client_version: str) -> str:
    return SOCKET_URL.format(csrf=socket_csrf(), ver=client_version)


class WhatnotClient:
    def __init__(self, transport: Transport = http_transport):
        self._t = transport
        self._feed_ids: dict[str, str] = {}

    def feed_id(self, slug: str) -> str:
        if slug not in self._feed_ids:
            data = self._t("GetLivestreamTagFeedId", Q_TAG_FEED_ID, {"name": slug})
            node = (data.get("data") or {}).get("getLivestreamTagByName")
            if not node:
                raise WhatnotError(f"unknown tag slug {slug!r}")
            self._feed_ids[slug] = node["feed"]["id"]
        return self._feed_ids[slug]

    def live_shows(self, feed_id: str, page_size: int = 50, max_pages: int = 12) -> list[dict]:
        """Every PLAYING stream in a category, biggest first, walking the feed's
        pages. One page of 24 used to be all the bot saw: measured live, the Pokemon
        feed had 600 streams and that page stopped at 145 viewers, so every small
        stream, where the odds are best, was invisible. objectSize above 50 returns
        nothing (measured), so 50 x 12 pages = 600."""
        out, seen, cursor = [], set(), None
        for _ in range(max(1, max_pages)):
            data = self._t("GetFeed", Q_GET_FEED, {
                "feedId": feed_id, "objectSize": page_size, "objectCursor": cursor,
                "filters": [{"field": "status", "values": ["PLAYING"]}],
                "sort": {"field": "VIEWER_COUNT", "direction": "DESC"}})
            objects = data["data"]["feed"]["objects"]
            for edge in objects["edges"]:
                obj = (edge.get("node") or {}).get("object") or {}
                if obj.get("__typename") == "LiveStream" and obj.get("status") == "PLAYING" and obj.get("id") not in seen:
                    seen.add(obj["id"])
                    out.append(obj)
            info = objects.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("endCursor"):
                break
            cursor = info["endCursor"]
        return out

    def seller_live_show(self, username: str) -> dict | None:
        data = self._t("GetUserLiveStreams", Q_USER_LIVE, {"username": username})
        user = (data.get("data") or {}).get("getUser")
        if not user or not user.get("isLive"):
            return None
        for edge in user.get("livestreams", {}).get("edges", []):
            node = edge["node"]
            if node.get("status") == "PLAYING":
                node = dict(node)
                node["user"] = {"id": user["id"], "username": user["username"]}
                return node
        return None

    def upcoming_giveaways(self, livestream_id: str) -> list[GiveawayListing]:
        data = self._t("LiveShopFeed", Q_LIVE_SHOP, {"liveId": livestream_id, "first": 40})
        out: list[GiveawayListing] = []
        feed = ((data.get("data") or {}).get("liveShop") or {}).get("feed") or {}
        for edge in feed.get("objects", {}).get("edges", []):
            node = edge.get("node") or {}
            if node.get("__typename") != "Section" or node.get("sectionType") != "SHOP_GIVEAWAYS":
                continue
            for e2 in node.get("contents", {}).get("edges", []):
                li = e2.get("node") or {}
                if li.get("transactionType") != "GIVEAWAY":
                    continue
                gp = ((li.get("transactionProps") or {}).get("giveaway")) or {}
                lp = next((sc["meta"]["id"] for sc in li.get("salesChannels", [])
                           if (sc.get("meta") or {}).get("type") == "LIVESTREAM_PRODUCT_ID"), None)
                if not lp:
                    continue
                out.append(GiveawayListing(
                    live_product_id=lp, listing_id=li["id"], title=(li.get("title") or "").strip(),
                    quantity=int(li.get("quantity") or 1), only_followers=bool(gp.get("onlyFollowers")),
                    only_domestic=bool(gp.get("onlyDomestic")), buyer_appreciation=bool(gp.get("buyerAppreciation"))))
        return out
