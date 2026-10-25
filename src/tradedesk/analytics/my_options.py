"""The owner's open Bybit option positions for the morning brief: P&L, theta per day and the chance of profit.

Pure (no I/O, time passed in). The input is the state file written by ``scripts/bybit_options_positions.py`` (a
read-only key; position/list for options plus the public option tickers), parsed as JSON; every value in it is
untrusted. Conventions are the desk's (docs/ARCHITECTURE.md): Bybit option expiries are 08:00 UTC like Deribit's;
Black-76 with r = 0 on the ticker's ``underlyingPrice`` (the expiry's forward), T = days / 365, IV = Bybit's ``markIv``
as a decimal (0.365 = 36.5 %). That model reproduces Bybit's own mark and theta per contract (live check 26.10, the
fixture tests/fixtures/bybit_option_tickers_btc_2026-10-26.json).

* ``theta_usd_day``: Black-76 theta per calendar day x signed size (a long loses it, a short earns it), USD.
* ``pop``: the risk-neutral probability that the leg finishes in profit at expiry, N(d2) at the breakeven (strike
  -/+ the average entry premium) under the leg's own mark IV (flat, no smile), fees ignored. Each leg on its own: a
  spread's legs are not combined. Risk-neutral = the market's price of insurance, not a forecast; not advice.
* ``upnl_usd``: Bybit's ``unrealisedPnl`` (mark against the average entry), else (mark - entry) x signed size.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from .instruments import parse_option_name
from .options import black76_greeks, d1d2, ncdf, year_fraction

SCHEMA = 1
MAX_AGE_HOURS = 3.0  # the fetch runs hourly; older positions may already be closed
MAX_POSITIONS = 50  # a sane cap on what the state file may hold
_PRODUCT = re.compile(r"^[A-Z0-9]{2,10}$")
_STRIKE = re.compile(r"^[0-9]{1,9}(d[0-9]{1,6})?$")  # Deribit-style: '81000', '0d55' (= 0.55)
_SETTLE = ("USDT", "USDC")
STATUS_REASONS = {
    "no_key": "no read-only Bybit key on this machine (/etc/tradedesk-bybit.env)",
    "key_not_read_only": "the Bybit key can trade: refused, positions not fetched",
    "error": "the Bybit request failed",
}


def _f(x: Any) -> float | None:
    """A finite float from a number or a numeric string, else None (exchange values are untrusted)."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_bybit_option(symbol: Any) -> tuple[str, datetime, float, bool, str] | None:
    """('BTC', expiry 08:00 UTC, strike, is_call, canonical symbol) for 'BTC-27NOV26-81000-C-USDT' or the USDC form
    'BTC-27NOV26-81000-C'; None for anything else. The symbol is rebuilt from the parsed parts, so it is safe text."""
    if not isinstance(symbol, str) or len(symbol) > 40:
        return None
    parts = symbol.split("-")
    if len(parts) == 5 and parts[4] in _SETTLE:
        settle = parts[4]
    elif len(parts) == 4:
        settle = ""
    else:
        return None
    o = parse_option_name("-".join(parts[:4]))
    if o is None or not _PRODUCT.match(o.product) or not _STRIKE.match(parts[2]) or o.strike <= 0:
        return None
    canon = f"{o.product}-{parts[1].upper()}-{parts[2]}-{'C' if o.is_call else 'P'}" + (f"-{settle}" if settle else "")
    return o.product, o.expiry, o.strike, o.is_call, canon


def prob_profit(F: float, K: float, T: float, sigma: float, is_call: bool, long: bool, premium: float) -> float | None:
    """Risk-neutral probability that one leg ends in profit at expiry (Black-76, r = 0, flat IV, fees ignored).

    Breakeven = K + premium for a call, K - premium for a put; P(F_T > X) = N(d2(F, X)). None without a usable input."""
    if F <= 0 or T <= 0 or sigma <= 0 or premium < 0:
        return None
    be = K + premium if is_call else K - premium
    if be <= 0:  # a put bought for more than its strike can never pay back; one sold that way always keeps a profit
        above = 1.0
    else:
        above = ncdf(d1d2(F, be, T, sigma)[1])
    win = above if is_call else 1.0 - above  # long call wins above, long put below the breakeven
    return win if long else 1.0 - win


def position_view(pos: Any, ticker: Any, asof: datetime) -> dict[str, Any] | None:
    """One open option leg as the brief shows it; None when the symbol, side or size is unusable."""
    if not isinstance(pos, dict):
        return None
    parsed = parse_bybit_option(pos.get("symbol"))
    side = pos.get("side")
    size = _f(pos.get("size"))
    if parsed is None or side not in ("Buy", "Sell") or size is None or size <= 0:
        return None
    base, expiry, strike, is_call, symbol = parsed
    long = side == "Buy"
    signed = size if long else -size
    t = ticker if isinstance(ticker, dict) else {}
    avg, mark = _f(pos.get("avgPrice")), _f(pos.get("markPrice"))
    if mark is None:
        mark = _f(t.get("markPrice"))
    iv, fwd = _f(t.get("markIv")), _f(t.get("underlyingPrice"))
    if fwd is None or fwd <= 0:
        fwd = _f(t.get("indexPrice"))
    upnl = _f(pos.get("unrealisedPnl"))
    if upnl is None and avg is not None and mark is not None:
        upnl = (mark - avg) * signed
    T = year_fraction(expiry, asof)
    theta = pop = None
    if fwd is not None and fwd > 0 and iv is not None and 0 < iv < 10 and T > 0:
        theta = black76_greeks(fwd, strike, T, iv, is_call)["theta"] * signed
        if avg is not None:
            pop = prob_profit(fwd, strike, T, iv, is_call, long, avg)
    be = None if avg is None else (strike + avg if is_call else strike - avg)
    return {
        "symbol": symbol,
        "base": base,
        "expiry_utc": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": round(T * 365.0, 3),
        "strike": strike,
        "kind": "call" if is_call else "put",
        "side": "long" if long else "short",
        "size": size,
        "avg_price": avg,
        "mark_price": mark,
        "iv": iv,
        "forward": fwd,
        "breakeven": be,
        "upnl_usd": upnl,
        "theta_usd_day": theta,
        "theta_exchange": _f(pos.get("theta")),  # Bybit's own figure, kept for comparison only
        "pop": pop,
    }


def my_options_view(state: Any, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    """The brief's view of the owner's open Bybit options: ``(data, None)`` or ``(None, reason)``.

    ``state`` is the parsed state file; ``now`` the snapshot time (for its age). Positions are valued at the file's own
    ``asof_utc`` (when the marks were taken). No open position is data (an empty list), not a failure. A file older
    than MAX_AGE_HOURS, a refused key or a failed fetch gives a reason instead. Positions are sorted by the size of
    their unrealised P&L."""
    if not isinstance(state, dict):
        return None, "no Bybit positions file yet (timer tradedesk-bybit-options off or no key)"
    status = state.get("status")
    if status != "ok":
        why = STATUS_REASONS.get(str(status), f"unknown status {str(status)[:20]!r}")
        detail = state.get("detail")
        return None, why + (f" ({str(detail)[:80]})" if isinstance(detail, str) and detail and status == "error" else "")
    try:
        asof = datetime.strptime(str(state.get("asof_utc")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None, "Bybit positions file without a readable asof_utc"
    age = (now - asof).total_seconds() / 3600
    if age > MAX_AGE_HOURS:
        return None, f"Bybit positions {age:.0f} h old (over {MAX_AGE_HOURS:.0f} h): timer tradedesk-bybit-options seems stopped"
    raw = state.get("positions")
    if not isinstance(raw, list):
        return None, "Bybit positions file without a positions list"
    tickers = state.get("tickers") if isinstance(state.get("tickers"), dict) else {}
    rows = []
    for p in raw[:MAX_POSITIONS]:
        sym = p.get("symbol") if isinstance(p, dict) else None
        v = position_view(p, tickers.get(sym) if isinstance(sym, str) else None, asof)
        if v is not None:
            rows.append(v)
    rows.sort(key=lambda r: -abs(r["upnl_usd"] or 0.0))
    upnl = [r["upnl_usd"] for r in rows if r["upnl_usd"] is not None]
    theta = [r["theta_usd_day"] for r in rows if r["theta_usd_day"] is not None]
    errs = state.get("errors") if isinstance(state.get("errors"), list) else []
    errors = [e[:120] for e in errs if isinstance(e, str)][:5]  # partial fetch failures (one coin), plain text
    return {
        "label": "risk_neutral",
        "source": "bybit",
        "asof_utc": asof.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_hours": round(age, 2),
        "n_positions": len(rows),
        "skipped": min(len(raw), MAX_POSITIONS) - len(rows),
        "upnl_usd": sum(upnl) if upnl else None,
        "n_upnl": len(upnl),
        "theta_usd_day": sum(theta) if theta else None,
        "n_theta": len(theta),
        "positions": rows,
        "errors": errors,
    }, None
