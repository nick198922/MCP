"""Delivery channels for the alerts engine.

Each channel takes a FiredAlert and returns ``(ok, error_or_none)``. The
scheduler job records the result into the ``alerts`` table so partial failures
(e.g. Telegram down) stay visible without blocking other channels.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..config import Settings
    from ..providers.telegram import Telegram
    from .engine import FiredAlert

log = logging.getLogger("tradedesk.alerts")


class Channel(Protocol):
    name: str

    async def send(self, alert: "FiredAlert") -> tuple[bool, str | None]: ...


class LogChannel:
    """Writes every alert to the regular logger. Always 'delivered'."""

    name = "log"

    async def send(self, alert: "FiredAlert") -> tuple[bool, str | None]:
        log.warning(
            "ALERT %s: value=%s op=%s threshold=%s metric=%s msg=%s",
            alert.rule_id,
            alert.value,
            alert.op,
            alert.threshold,
            alert.metric,
            alert.message,
        )
        return True, None


class TelegramChannel:
    name = "telegram"

    def __init__(self, client: "Telegram", chat_id: str, thread_id: int | None = None) -> None:
        self._client = client
        self._chat_id = chat_id
        self._thread_id = thread_id  # a forum topic of the group, if it has topics

    async def send(self, alert: "FiredAlert") -> tuple[bool, str | None]:
        text = alert.message or (f"[{alert.rule_id}] value={alert.value} op={alert.op} threshold={alert.threshold}")
        try:
            await self._client.send_message(self._chat_id, text, message_thread_id=self._thread_id)
        except Exception as e:  # noqa: BLE001 — a bad bot shouldn't crash the scheduler
            return False, f"{type(e).__name__}: {e}"
        return True, None


def build_channels(settings: "Settings") -> list[Channel]:
    channels: list[Channel] = [LogChannel()]
    if settings.telegram_bot_token and settings.telegram_chat_id:
        # Imported here so the Telegram HTTP client only spins up when needed.
        from ..core.http import Http  # noqa: PLC0415
        from ..providers.telegram import BASE_URL as TG_URL  # noqa: PLC0415
        from ..providers.telegram import Telegram  # noqa: PLC0415

        # sendMessage is not idempotent: a retry after a read timeout or a 5xx can post the same alert twice (Telegram
        # may have delivered it before the answer was lost). So retries cover only failures where the request surely
        # did not reach Telegram: connect / DNS / TLS errors and HTTP 429 (flood control, honours retry_after).
        # A lost alert is still recorded as failed in the `alerts` table; a duplicate would not be visible anywhere.
        http = Http(
            "telegram",
            TG_URL,
            rate=1.0,
            burst=2,
            timeout=settings.http_timeout,
            retries=settings.http_retries,
            backoff_base=settings.http_retry_backoff,
            retry_unsent_only=True,
            # Exact-string masking, so the token never reaches an error (and the `alerts` table) even outside `/bot<token>`.
            secrets=(settings.telegram_bot_token,),
        )
        channels.append(
            TelegramChannel(Telegram(http, settings.telegram_bot_token), settings.telegram_chat_id, settings.telegram_topic_alerts)
        )
    return channels
