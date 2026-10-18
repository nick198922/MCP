"""Upbit announcements as a risk filter: recent KRW listings, trading-caution warnings and delistings per token.

Pure functions (no I/O); time is passed in. Titles are classified by ``news_events.classify_upbit`` (fixed patterns on
untrusted text; nothing in a title or body is ever executed). This is a filter for other trades, not a signal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .macro_calendar import clean_text
from .news_events import KST_OFFSET_MS, _upbit_tickers, classify_upbit, upbit_time_ms

KINDS = ("listing", "warning", "delist")
# Extensions / releases of a trading-caution designation are "ignore" for the shared classifier (stage 0c research);
# here they are recognised separately: Upbit extends warnings month by month, so the original announcement falls out
# of the window while the coin is still under warning.
WARNING_STATES = ("warning", "warning_extended", "warning_released")
_QUOTES = frozenset({"USDT", "USDC", "KRW", "USD", "BTC"})


def normalize_token(token: str | None) -> str | None:
    """Base ticker of a user-supplied symbol: 'doge' / 'DOGE/USDT' / 'KRW-DOGE' / 'DOGEUSDT' -> 'DOGE'."""
    if not token or not token.strip():
        return None
    t = token.upper().strip()
    parts = [p for p in re.split(r"[/\-_:\s]+", t) if p]
    if len(parts) > 1:
        base = [p for p in parts if p not in _QUOTES]
        return base[0] if base else parts[0]
    t = parts[0] if parts else t
    for q in ("USDT", "USDC", "KRW", "USD"):
        if t.endswith(q) and len(t) - len(q) >= 2:
            return t[: -len(q)]
    return t


def _valid_id(nid: Any) -> bool:
    """Ids come from untrusted JSON: only plain ints / strings are usable as keys."""
    return isinstance(nid, int | str) and not isinstance(nid, bool)


def _warning_state(title: str) -> str | None:
    """'warning_extended' / 'warning_released' for non-batch titles of an extension / release of a designation."""
    t = " ".join(title.split())
    if "유의" not in t or "지정" not in t or re.search(r"\d+\s*종", t):
        return None
    if "연장" in t:
        return "warning_extended"
    if "해제" in t:
        return "warning_released"
    return None


def _iso(ms: int, offset_ms: int = 0) -> str:
    """UTC ISO time with offset (``offset_ms=0``), or a naive wall-clock string shifted by ``offset_ms`` (KST)."""
    d = datetime.fromtimestamp((ms + offset_ms) / 1000, UTC)
    return (d if offset_ms == 0 else d.replace(tzinfo=None)).isoformat(timespec="seconds")


def batch_candidates(notices: Iterable[Mapping[str, Any]], now: datetime, days: int) -> list[Mapping[str, Any]]:
    """Notices in the window whose title is a batch ("(8종)"): the tickers are only in the body."""
    lo, hi = _window(now, days)
    out = []
    for n in notices:
        ms = upbit_time_ms(n)
        if _valid_id(n.get("id")) and ms is not None and lo <= ms <= hi and classify_upbit(str(n.get("title") or ""))[0].endswith("_batch"):
            out.append(n)
    return out


def _window(now: datetime, days: int) -> tuple[int, int]:
    hi = int(now.timestamp() * 1000)
    return int((now - timedelta(days=days)).timestamp() * 1000), hi


def summarize(
    notices: Sequence[Mapping[str, Any]],
    now: datetime,
    days: int,
    *,
    token: str | None = None,
    body_tickers: Mapping[Any, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Events of the last ``days`` days: ``kind`` (listing | warning | delist), tickers, publication time.

    ``body_tickers`` maps a batch notice id to the tickers parsed from its body (empty: the body repeats an earlier
    decision, the notice is dropped). Batch notices not in the map go to ``unresolved_batches``. With ``token`` only
    events naming it are returned (unresolved batches stay listed: they may include it). Newest first."""
    tok = normalize_token(token)
    lo, hi = _window(now, days)
    events: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for n in notices:
        nid = n.get("id")
        if not _valid_id(nid) or nid in seen:
            continue
        seen.add(nid)
        ms = upbit_time_ms(n)
        if ms is None or not (lo <= ms <= hi):
            continue
        title = str(n.get("title") or "")
        kind, ticks = classify_upbit(title)
        if kind == "ignore":
            state = _warning_state(title)
            if state:
                kind, ticks = state, _upbit_tickers(" ".join(title.split()))
        batch = kind.endswith("_batch")
        if batch:
            kind = kind[: -len("_batch")]
            if body_tickers is None or nid not in body_tickers:
                unresolved.append({"id": nid, "kind": kind, "published_utc": _iso(ms), "title": clean_text(title, 200)})
                continue
            ticks = list(body_tickers[nid])
        if (kind not in KINDS and kind not in WARNING_STATES) or not ticks:
            continue
        if tok is not None and tok not in ticks:
            continue
        events.append(
            {
                "id": nid,
                "kind": kind,
                "tickers": ticks,
                "published_utc": _iso(ms),
                "published_kst": _iso(ms, KST_OFFSET_MS),
                "age_days": round((hi - ms) / 86_400_000, 2),
                "from_batch_body": batch,
                "title": clean_text(title, 200),
            }
        )
    events.sort(key=lambda e: e["published_utc"], reverse=True)
    unresolved.sort(key=lambda e: e["published_utc"], reverse=True)
    out: dict[str, Any] = {"events": events, "unresolved_batches": unresolved}
    if tok is not None:
        out["token"] = tok
        # None = unknown: an unresolved batch notice of that kind may name the token
        pending = {u["kind"] for u in unresolved}

        def flag(found: bool, kind: str) -> bool | None:
            return True if found else (None if kind in pending else False)

        has = {k: any(e["kind"] == k for e in events) for k in (*KINDS, *WARNING_STATES)}
        out["recent_listing"] = flag(has["listing"], "listing")
        out["recent_delist"] = flag(has["delist"], "delist")
        out["recent_warning"] = flag(has["warning"] or has["warning_extended"], "warning")
        out["recent_warning_released"] = has["warning_released"]
        # the newest designation / extension / release notice decides (events are newest first)
        state = next((e["kind"] for e in events if e["kind"] in WARNING_STATES), None)
        out["warning_active"] = flag(state in ("warning", "warning_extended"), "warning")
    return out
