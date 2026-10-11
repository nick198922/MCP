"""Telegram Bot API (sendMessage only).

A new instance is built on demand when the alert engine actually has a bot
token; the Runtime does not keep one around just in case. Rate limit is kept
conservative (Telegram allows ~30 msg/s but per-chat ~1/s).
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.telegram.org"


class Telegram:
    name = "telegram"

    def __init__(self, http: Http, bot_token: str) -> None:
        self.http = http
        self._token = bot_token

    async def aclose(self) -> None:
        await self.http.aclose()

    async def send_message(self, chat_id: str, text: str, *, parse_mode: str | None = None) -> Fetched:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            body["parse_mode"] = parse_mode
        f = await self.http.post_json(f"/bot{self._token}/sendMessage", json=body, ttl=0, tool="sendMessage", args={"chat_id": chat_id})
        if not isinstance(f.data, dict) or not f.data.get("ok"):
            raise ProviderError(self.name, "backend_error", f"telegram rejected: {f.data!r}")
        return f
