"""Discord reporting via the REST API (same approach as ../claude_chat.py, no discord.py)."""
from __future__ import annotations

import json
import logging
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import DiscordCfg

log = logging.getLogger(__name__)
API = "https://discord.com/api/v10"
COMMANDS = {"status", "pause", "resume"}


class DiscordHttp:
    def __init__(self, token: str, channel_id: str):
        self.token = token
        self.channel_id = channel_id

    def _request(self, method: str, path: str, params=None, body: bytes | None = None, content_type: str | None = None):
        url = f"{API}{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
        headers = {"Authorization": f"Bot {self.token}", "User-Agent": "DiscordBot (https://github.com/local/givvy, 1.0)"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode("utf-8") or "null")
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(min(2 ** attempt, 60))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 5:
                    time.sleep(min(2 ** attempt, 60))
                    continue
                raise

    def post_message(self, content: str, file: str | None = None):
        path = f"/channels/{self.channel_id}/messages"
        if not file:
            return self._request("POST", path, body=json.dumps({"content": content}).encode(), content_type="application/json")
        boundary = uuid.uuid4().hex
        p = Path(file)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n"
            + json.dumps({"content": content, "attachments": [{"id": 0, "filename": p.name}]}) + "\r\n",
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{p.name}\"\r\nContent-Type: {ctype}\r\n\r\n",
        ]
        body = parts[0].encode() + parts[1].encode() + p.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        return self._request("POST", path, body=body, content_type=f"multipart/form-data; boundary={boundary}")

    def get_messages(self, after: str | None):
        params = {"limit": 50}
        if after:
            params["after"] = after
        return self._request("GET", f"/channels/{self.channel_id}/messages", params=params) or []


def format_entry(prize: str, seller: str, game: str, entries: int | None, score: float) -> str:
    return (f"Entered: {prize or '(unknown prize)'} — {seller} ({game}, "
            f"{entries if entries is not None else '?'} entries, score {score:.1f})")


def format_status(summary: dict, device: str | None, paused: bool, watching: int) -> str:
    state = "paused" if paused else ("running on " + device if device else "idle, no device")
    return (f"Status: {state}, watching {watching} shows. Last 24h: {summary['entries']} entered, "
            f"{summary['missed']} missed, {summary['giveaways_seen']} giveaways seen, {summary['wins']} wins, "
            f"{summary['follows']} follows, {summary['errors']} errors.")


class Notifier:
    def __init__(self, cfg: DiscordCfg, http=None, skip_history: bool = False):
        self.cfg = cfg
        self.enabled = bool(cfg.bot_token and cfg.channel_id)
        self.http = http if http is not None else (DiscordHttp(cfg.bot_token, cfg.channel_id) if self.enabled else None)
        self.last_id: str | None = None
        self.skip_history = skip_history   # main.py passes True so old commands aren't replayed at startup
        self._primed = False

    def plain(self, text: str, file: str | None = None):
        log.info("discord: %s", text)
        if not self.enabled:
            return
        try:
            self.http.post_message(text[:1900], file)
        except Exception as e:
            log.warning("discord send failed: %s", e)

    def alert(self, text: str, file: str | None = None):
        self.plain(f"{self.cfg.mention} {text}", file)

    def poll_commands(self) -> list[str]:
        if not self.enabled:
            return []
        try:
            msgs = self.http.get_messages(self.last_id)
        except Exception as e:
            log.warning("discord poll failed: %s", e)
            return []
        msgs = sorted(msgs, key=lambda m: int(m["id"]))
        out = []
        for m in msgs:
            self.last_id = m["id"]
            if self.skip_history and not self._primed:
                continue          # first poll after startup: move the cursor past old messages only
            if not m.get("author", {}).get("bot"):
                text = (m.get("content") or "").strip().lower()
                if text.startswith("!") and text[1:] in COMMANDS:
                    out.append(text[1:])
        self._primed = True
        return out
