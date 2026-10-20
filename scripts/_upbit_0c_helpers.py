"""Archived helpers of the removed Upbit provider, vendored for scripts/verify_news_events_0c.py (history only).

The Upbit provider, tool and analytics were removed from the project on 2026-10-19 (owner decision: Korean exchange
listings are not worth trading). These functions were moved verbatim from ``tradedesk.analytics.news_events``; they
are no longer tested or maintained.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime

from tradedesk.analytics.news_events import STABLES

# ------------------------------------------------------------------ stage 0c: Upbit announcements (Korean titles)

KST_OFFSET_MS = 9 * 3_600_000

_UP_GROUP = re.compile(r"\(([^()]*)\)")  # innermost parenthesised groups
# one item of a group: a bare ticker ("SOL") or a Korean name + space + ticker ("도지코인 DOGE", "리니어 파이낸스 LINA")
_UP_ITEM = re.compile(r"^(?:[^,()]*[가-힣][^,()]*\s+)?([A-Z0-9]{1,15})$")
_UP_LEAD = re.compile(r"^([A-Z0-9]{2,15}(?:\s*,\s*[A-Z0-9]{2,15})*)\s+(?=[가-힣])")  # "LUNA 유의 종목 지정 안내"
_UP_BATCH = re.compile(r"\d+\s*종")  # "(18종)", "24종 디지털 자산": the tickers are only in the body
_UP_MARKET_WORDS = frozenset({"KRW", "BTC", "USDT", "ETH"})
UPBIT_STABLES = frozenset({"KRW", "USDG", "RLUSD", "EURC", "USDQ", "USD0", "GHO", "FRAX", "LUSD", "JPYC"})
_UP_LISTING = ("디지털 자산 추가", "신규 거래지원", "신규 거래 지원", "신규 상장", "코인 추가")
_UP_KRW = re.compile(r"KRW|원화\s*마켓")


def _upbit_tickers(t: str) -> list[str]:
    """Tickers of a title: items of parenthesised groups (a group naming markets -- it contains "마켓" -- is skipped);
    if there are none, a leading comma-separated list of upper-case symbols followed by Korean text."""
    out: list[str] = []
    for g in _UP_GROUP.findall(t):
        if "마켓" in g:
            continue
        for part in g.split(","):
            m = _UP_ITEM.match(part.strip())
            if m:
                out.append(m.group(1))
    if not out:
        m = _UP_LEAD.match(t)
        if m and not t[m.end() :].startswith("마켓"):
            out = [x.strip() for x in m.group(1).split(",")]
    return [x for x in dict.fromkeys(out) if x not in STABLES and x not in UPBIT_STABLES and x not in _UP_MARKET_WORDS]


def _upbit_main(t: str) -> str:
    """The title without trailing parenthesised update notes ("... (거래지원 종료 일정 변경 안내)") added when a notice
    is edited; the publication time of the notice stays the original one."""
    return re.sub(r"(?:\s*\([^()]*\))+\s*$", "", t)


def upbit_body_tickers(text: str) -> list[str]:
    """Tickers of the numbered list of a batch notice body ("**1) 시린토큰(SRN)**", "2) Cred(LBA)"), in order, without
    duplicates (the English copy repeats them) and without market / stable symbols; if the body has no numbered list,
    every "name(TICK)". Assets merely mentioned in the text (released, kept on the list, "(KST)") are not in the
    numbered list. The body is untrusted text: only these fixed patterns are matched."""
    found = re.findall(r"(?m)^[*\s]*\d+\)\s*[^()\n]*?\(([A-Z0-9]{2,15})\)", text)
    if not found:
        found = re.findall(r"[가-힣A-Za-z0-9.]\s?\(([A-Z0-9]{2,15})\)", text)
    skip = STABLES | UPBIT_STABLES | _UP_MARKET_WORDS | {"KST", "UTC"}
    # an asset whose designation is extended / kept / released or whose delisting is put on hold ("베이직(BASIC)은
    # 유의종목 지정이 연장됩니다") is not part of the batch decision, even if the body numbers it
    for line in text.splitlines():
        if re.search(r"연장|유지|해제|보류", line) and not re.match(r"^[*\s]*\d+\)", line):
            skip |= set(re.findall(r"\(([A-Z0-9]{2,15})\)", line))
    return [x for x in dict.fromkeys(found) if x not in skip]


def upbit_body_is_reminder(text: str) -> bool:
    """A notice that repeats an earlier decision ("...일정을 다시 한 번 안내드리오니"): an update, not a new event."""
    return "다시 한 번 안내" in text


def upbit_batch_size(title: str) -> int | None:
    """N of a batch notice title ("디지털 자산 유의 종목 지정 안내 (8종)" -> 8); None if the title is not a batch."""
    m = re.search(r"(\d+)\s*종", title)
    return int(m.group(1)) if m else None


def upbit_time_ms(notice: Mapping[str, object]) -> int | None:
    """Publication time (epoch ms UTC) of an Upbit notice: ``first_listed_at`` if present, else ``listed_at``.
    ISO strings with an offset are honoured; naive strings are KST (UTC+9)."""
    for k in ("first_listed_at", "listed_at"):
        s = notice.get(k)
        if not s:
            continue
        try:
            d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if d.tzinfo is None:
            return int(d.replace(tzinfo=UTC).timestamp() * 1000) - KST_OFFSET_MS
        return int(d.timestamp() * 1000)
    return None


def classify_upbit(title: str) -> tuple[str, list[str]]:
    """Kind and tickers of an Upbit ``trade`` notice title (untrusted text, fixed patterns only).

    Kinds: ``listing`` (a KRW market is added; "KRW" or "원화 마켓", wordings of 2019-2026), ``warning``
    (designated as a trading-caution asset; not an extension, release or a mere caution call), ``delist`` (trading
    support ends for the asset, not just one market), ``warning_batch`` / ``delist_batch`` (a batch notice, "(8종)":
    the tickers are only in the body), ``ignore``. Tickers: items of parenthesised groups (bare or "name TICKER",
    comma-separated), else a leading upper-case list. A listing title that also designates another asset as a
    warning ("... 추가 (T) 및 이에 따른 NU 유의 종목 지정") is a listing of the parenthesised ticker."""
    t = " ".join(title.split())
    main = _upbit_main(t)
    ticks = _upbit_tickers(t)
    if _UP_KRW.search(t) and any(x in t for x in _UP_LISTING) and "종료" not in main:
        return ("listing", ticks) if ticks else ("ignore", [])
    if re.search(r"거래\s?지원 종료", t):
        if re.search(r"마켓\s*(?:디지털 자산\s*)?거래\s?지원 종료", t) or "페어 제거" in t:
            return "ignore", []  # one market only
        if any(x in main for x in ("연기", "철회", "취소", "변경", "정정")):
            return "ignore", []  # postponed / withdrawn / date changed / corrected: an update, not a new decision
        if _UP_BATCH.search(t):
            return "delist_batch", []
        return ("delist", ticks) if ticks else ("ignore", [])
    if "유의" in t and "지정" in t:
        if any(x in t for x in ("연장", "해제", "촉구", "유지")):
            return "ignore", []
        if _UP_BATCH.search(t):
            return "warning_batch", []
        return ("warning", ticks) if ticks else ("ignore", [])
    return "ignore", []


def btc_cross_ratio(perp: float, perp_btc: float, krw_x: float, krw_btc: float) -> float | None:
    """Ticker-identity check across venues: (perp price / BTC perp price) / (Upbit KRW-X / Upbit KRW-BTC), i.e. the
    token's price in BTC on both venues. The KRW premium ("kimchi premium") and the FX rate cancel; a ratio far from 1
    means the same ticker is a different token. None if any price is missing or not positive."""
    if not all(x is not None and x > 0 for x in (perp, perp_btc, krw_x, krw_btc)):
        return None
    return (perp / perp_btc) / (krw_x / krw_btc)
