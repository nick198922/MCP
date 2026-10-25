"""Fetch the owner's open Bybit option positions into a local file for the assistant's brief. Read-only.

    uv run python scripts/bybit_options_positions.py                 # write /var/lib/tradedesk-mcp/assistant/my_options.json
    uv run python scripts/bybit_options_positions.py --print         # also print the brief's view of it

Docs: docs/ideas/IDEA-20261018-assistant.md («Відкриті опціонні позиції власника на Bybit»). The key pair is read from
the env file (default /etc/tradedesk-bybit.env, the same as scripts/bybit_import.py; never from the command line, never
printed, never written to the output). Only GET endpoints are called: user/query-api (the key must be read-only, else
nothing else is asked), position/list with category=option (per base coin and settle coin, as bybit_import's
options-probe), and the public market/tickers for options (mark IV and the expiry's forward). Nothing places, changes
or cancels an order. The output keeps a fixed set of position fields and, on a failure, only a status and the error's
class name (an exchange message is not copied: it is untrusted and might echo request details). The collector job
assistant_snapshot reads the file as the section ``my_options`` (no key there); the MCP server never reads the key.
Scheduled hourly by deploy/user-units/tradedesk-bybit-options.* while the env file exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import bybit_import  # noqa: E402

from tradedesk.analytics.my_options import SCHEMA, my_options_view  # noqa: E402
from tradedesk.collector.assistant_snapshot import write_atomic  # noqa: E402

ENV_DEFAULT = "/etc/tradedesk-bybit.env"
OUT_DEFAULT = Path("/var/lib/tradedesk-mcp/assistant/my_options.json")
BASE_COINS = ("BTC", "ETH", "SOL")
SETTLE_COINS = ("USDT", "USDC")
POSITION_FIELDS = ("symbol", "side", "size", "avgPrice", "markPrice", "unrealisedPnl", "positionValue", "delta", "theta")
TICKER_FIELDS = ("markPrice", "markIv", "underlyingPrice", "indexPrice")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(status: str, detail: str = "") -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "source": "bybit",
        "asof_utc": _now(),
        "status": status,
        "detail": detail,
        "positions": [],
        "tickers": {},
        "errors": [],
    }


def _size(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _client(env: str) -> Any | None:
    """A ccxt Bybit client with the read-only key, or None when the env file or a key is missing."""
    try:
        key, secret = bybit_import.read_env(env)
    except (OSError, SystemExit):  # read_env exits on a missing key; nothing about the key is printed
        return None
    import ccxt  # noqa: PLC0415

    return ccxt.bybit({"apiKey": key, "secret": secret, "enableRateLimit": True})


def collect(ex: Any) -> dict[str, Any]:
    """The state file's payload from a client (``ex``: ccxt bybit or a fake with the same three methods). Never raises."""
    try:
        res = (ex.privateGetV5UserQueryApi() or {}).get("result") or {}
    except Exception as e:  # noqa: BLE001 - a refused key is a status, not a crash
        return _status("error", f"user/query-api: {type(e).__name__}")
    if str(res.get("readOnly")) != "1":
        return _status("key_not_read_only")
    out = _status("ok")
    seen: dict[str, dict[str, Any]] = {}
    calls = [{"category": "option", "baseCoin": c} for c in BASE_COINS] + [{"category": "option", "settleCoin": c} for c in SETTLE_COINS]
    failed = 0
    for params in calls:
        try:
            rows = bybit_import._pages(ex.privateGetV5PositionList, params)
        except Exception as e:  # noqa: BLE001 - one coin failing must not hide the rest
            failed += 1
            out["errors"].append(f"position/list {params.get('baseCoin') or params.get('settleCoin')}: {type(e).__name__}")
            continue
        for x in rows:
            sym = x.get("symbol")
            if isinstance(sym, str) and sym not in seen and _size(x.get("size")) != 0:
                seen[sym] = {k: x.get(k) for k in POSITION_FIELDS}
    if failed == len(calls):
        return _status("error", "position/list: every request failed")
    out["positions"] = list(seen.values())
    bases = sorted({b for b in (s.split("-")[0] for s in seen) if b.isascii() and b.isalnum() and b.isupper() and len(b) <= 10})
    for base in bases:
        try:
            lst = ((ex.publicGetV5MarketTickers({"category": "option", "baseCoin": base}) or {}).get("result") or {}).get("list") or []
        except Exception as e:  # noqa: BLE001 - without tickers P&L still shows; theta and chance stay empty
            out["errors"].append(f"market/tickers {base}: {type(e).__name__}")
            continue
        for t in lst:
            if isinstance(t, dict) and t.get("symbol") in seen:
                out["tickers"][t["symbol"]] = {k: t.get(k) for k in TICKER_FIELDS}
    out["asof_utc"] = _now()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", default=ENV_DEFAULT, help="env file with BYBIT_API_KEY / BYBIT_API_SECRET (read-only key)")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT, help="state file (the collector reads <dir of db>/assistant/my_options.json)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="also print the brief's view (no key, no raw data)")
    a = ap.parse_args(argv)
    ex = _client(a.env)
    payload = _status("no_key") if ex is None else collect(ex)
    write_atomic(a.out, payload)
    data, why = my_options_view(payload, datetime.now(UTC))
    if a.print_only:
        print(json.dumps(data if data is not None else {"reason": why}, ensure_ascii=False, indent=1))
    else:
        n = data["n_positions"] if data else 0
        print(f"bybit options: status {payload['status']}, open positions {n}, errors {len(payload['errors'])}")
    return 0 if payload["status"] in ("ok", "no_key") else 1


if __name__ == "__main__":
    sys.exit(main())
