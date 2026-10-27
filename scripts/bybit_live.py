"""Live state of the owner's Bybit account for the dashboard's «Позиції» tab. Read-only.

    uv run python scripts/bybit_live.py                  # one pass: write /var/lib/tradedesk-mcp/assistant/live_positions.json
    uv run python scripts/bybit_live.py --run-for 55     # keep refreshing for 55 s (the minutely timer's mode)

Every ``--tickers-every`` seconds (default 10) the public market/tickers of the open positions' markets are refreshed;
every ``--positions-every`` seconds (default 30) also position/list (options per base and settle coin, linear per settle
coin), the unified wallet and the realised 7/30-day volatility of each option base (public daily klines). The key pair comes from the env file (default /etc/tradedesk-bybit.env, the same as
scripts/bybit_options_positions.py) and is never printed or written. The key must be read-only (user/query-api), else
nothing else is asked. Only GET endpoints are called; nothing places, changes or cancels an order. The output keeps a
fixed set of fields; a failure keeps only a status and the error's class name (an exchange message is untrusted).
The dashboard computes P&L, greeks and breakevens from this file (scripts/dashboard.py, «Позиції»). Scheduled every
minute by deploy/user-units/tradedesk-bybit-live.* while the env file exists.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import bybit_import  # noqa: E402
import bybit_options_positions as bop  # noqa: E402

from tradedesk.collector.assistant_snapshot import write_atomic  # noqa: E402

SCHEMA = 1
OUT_DEFAULT = Path("/var/lib/tradedesk-mcp/assistant/live_positions.json")
POSITION_FIELDS = (
    "symbol",
    "side",
    "size",
    "avgPrice",
    "markPrice",
    "unrealisedPnl",
    "curRealisedPnl",
    "positionValue",
    "delta",
    "gamma",
    "vega",
    "theta",
    "leverage",
    "liqPrice",
    "breakEvenPrice",
    "openTime",
    "createdTime",
)
TICKER_FIELDS = ("markPrice", "markIv", "underlyingPrice", "indexPrice", "bid1Price", "ask1Price", "lastPrice")
WALLET_FIELDS = (
    "totalEquity",
    "totalWalletBalance",
    "totalAvailableBalance",
    "totalInitialMargin",
    "totalMaintenanceMargin",
    "totalPerpUPL",
)
POSITION_CALLS = [{"category": "option", "baseCoin": c} for c in bop.BASE_COINS] + [
    {"category": c, "settleCoin": s} for c in ("option", "linear") for s in bop.SETTLE_COINS
]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty(status: str, detail: str = "") -> dict[str, Any]:
    return {"schema": SCHEMA, "source": "bybit", "asof_utc": _now(), "positions_utc": None, "status": status, "detail": detail,
            "positions": [], "tickers": {}, "wallet": {}, "rv": {}, "errors": []}  # fmt: skip


def key_ok(ex: Any) -> str | None:
    """None when the key is read-only, else the status to write."""
    try:
        res = (ex.privateGetV5UserQueryApi() or {}).get("result") or {}
    except Exception:  # noqa: BLE001 - a refused key is a status, not a crash
        return "error"
    return None if str(res.get("readOnly")) == "1" else "key_not_read_only"


def fetch_positions(ex: Any) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Open positions (fixed fields + ``category``), or None when every request failed; plus error notes."""
    seen: dict[str, dict[str, Any]] = {}
    errors, failed = [], 0
    for params in POSITION_CALLS:
        try:
            rows = bybit_import._pages(ex.privateGetV5PositionList, params)
        except Exception as e:  # noqa: BLE001 - one request failing must not hide the rest
            failed += 1
            errors.append(f"position/list {params['category']} {params.get('baseCoin') or params.get('settleCoin')}: {type(e).__name__}")
            continue
        for x in rows:
            sym = x.get("symbol")
            if isinstance(sym, str) and sym not in seen and bop._size(x.get("size")) != 0:
                seen[sym] = {k: x.get(k) for k in POSITION_FIELDS} | {"category": params["category"]}
    return (None if failed == len(POSITION_CALLS) else list(seen.values())), errors


def fetch_wallet(ex: Any) -> tuple[dict[str, Any], list[str]]:
    try:
        lst = ((ex.privateGetV5AccountWalletBalance({"accountType": "UNIFIED"}) or {}).get("result") or {}).get("list") or []
    except Exception as e:  # noqa: BLE001 - positions still show without the wallet
        return {}, [f"wallet-balance: {type(e).__name__}"]
    acc = lst[0] if lst and isinstance(lst[0], dict) else {}
    return {k: acc.get(k) for k in WALLET_FIELDS}, []


def _option_base(sym: str) -> str | None:
    b = sym.split("-")[0]
    return b if b.isascii() and b.isalnum() and b.isupper() and len(b) <= 10 else None


def fetch_tickers(ex: Any, positions: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Public tickers of the positions' markets: one request per option base coin, one per linear symbol."""
    want = {p["symbol"] for p in positions}
    out: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    calls = [
        {"category": "option", "baseCoin": b}
        for b in sorted({_option_base(p["symbol"]) for p in positions if p["category"] == "option"} - {None})
    ]
    calls += [{"category": "linear", "symbol": p["symbol"]} for p in positions if p["category"] == "linear"]
    for params in calls:
        try:
            lst = ((ex.publicGetV5MarketTickers(params) or {}).get("result") or {}).get("list") or []
        except Exception as e:  # noqa: BLE001 - without a ticker the page falls back to the position's mark
            errors.append(f"market/tickers {params.get('baseCoin') or params.get('symbol')}: {type(e).__name__}")
            continue
        for t in lst:
            if isinstance(t, dict) and t.get("symbol") in want:
                out[t["symbol"]] = {k: t.get(k) for k in TICKER_FIELDS}
    return out, errors


def realised_vol(closes: list[float], days: int) -> float | None:
    """Annualised close-to-close volatility (decimal) of the last ``days`` daily log returns; None when too short."""
    if len(closes) < days + 1 or any(c <= 0 for c in closes[-days - 1 :]):
        return None
    tail = closes[-days - 1 :]
    rets = [math.log(b / a) for a, b in zip(tail, tail[1:], strict=False)]
    return statistics.pstdev(rets) * math.sqrt(365.0)


def fetch_rv(ex: Any, positions: list[dict[str, Any]]) -> tuple[dict[str, dict[str, float | None]], list[str]]:
    """Realised 7- and 30-day volatility of each option base from Bybit's public daily klines of <base>USDT (linear),
    complete days only: the market's actual move to set against the theta the options cost."""
    out: dict[str, dict[str, float | None]] = {}
    errors: list[str] = []
    for base in sorted({_option_base(p["symbol"]) for p in positions if p["category"] == "option"} - {None}):
        try:
            rows = (
                (ex.publicGetV5MarketKline({"category": "linear", "symbol": f"{base}USDT", "interval": "D", "limit": 32}) or {}).get(
                    "result"
                )
                or {}
            ).get("list") or []
            closes = [float(r[4]) for r in sorted(rows[1:], key=lambda r: int(r[0]))]  # newest first; row 0 is today, unfinished
        except Exception as e:  # noqa: BLE001 - the page shows the greeks without the realised move
            errors.append(f"market/kline {base}: {type(e).__name__}")
            continue
        out[base] = {"rv7": realised_vol(closes, 7), "rv30": realised_vol(closes, 30)}
    return out, errors


def run(ex: Any, out: Path, run_for: float, tickers_every: float, positions_every: float, clock=time.monotonic, sleep=time.sleep) -> str:
    """Refresh the file until ``run_for`` seconds have passed (one pass when 0). Returns the last status. Never raises."""
    bad = key_ok(ex)
    if bad:
        write_atomic(out, empty(bad, "user/query-api"))
        return bad
    start, last_pos = clock(), -1e18
    state = empty("ok")
    while True:
        t = clock()
        errors: list[str] = []
        if t - last_pos >= positions_every:
            positions, errs = fetch_positions(ex)
            errors += errs
            if positions is None:
                state = empty("error", "position/list: every request failed")
            else:
                wallet, errs = fetch_wallet(ex)
                errors += errs
                rv, errs = fetch_rv(ex, positions)
                errors += errs
                state.update(status="ok", detail="", positions=positions, wallet=wallet, rv=rv, positions_utc=_now())
            last_pos = t
        if state["status"] == "ok":
            tickers, errs = fetch_tickers(ex, state["positions"])
            errors += errs
            state["tickers"] = tickers
        state["errors"], state["asof_utc"] = errors, _now()
        write_atomic(out, state)
        if clock() - start + tickers_every > run_for:
            return state["status"]
        sleep(max(0.0, tickers_every - (clock() - t)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", default=bop.ENV_DEFAULT, help="env file with BYBIT_API_KEY / BYBIT_API_SECRET (read-only key)")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--run-for", type=float, default=0.0, help="seconds to keep refreshing (0 = one pass)")
    ap.add_argument("--tickers-every", type=float, default=10.0)
    ap.add_argument("--positions-every", type=float, default=30.0)
    a = ap.parse_args(argv)
    ex = bop._client(a.env)
    if ex is None:
        write_atomic(a.out, empty("no_key"))
        return 0
    status = run(ex, a.out, a.run_for, max(a.tickers_every, 2.0), max(a.positions_every, 5.0))
    print(f"bybit live: status {status}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
