"""Import the owner's Binance USD-M futures history into the trade journal. Read-only.

    uv run python scripts/binance_import.py --env /etc/tradedesk-binance.env check

The key pair is read from the env file (never from the command line, never printed). `check` asks Binance for the key's
own restrictions and refuses any key that can trade, transfer or withdraw. The import itself (income history and the
asynchronous trade-history downloads, which reach further back than the 6-month trade endpoint) is added next.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

DANGEROUS = [
    "enableWithdrawals",
    "enableInternalTransfer",
    "permitsUniversalTransfer",
    "enableMargin",
    "enableFutures",
    "enableSpotAndMarginTrading",
    "enableVanillaOptions",
    "enablePortfolioMarginTrading",
    "enableFixApiTrade",
]


def client(env: str) -> Any:
    import ccxt  # noqa: PLC0415

    vals = dict(line.split("=", 1) for line in Path(env).read_text().splitlines() if "=" in line and not line.startswith("#"))
    key, secret = vals.get("BINANCE_API_KEY", "").strip(), vals.get("BINANCE_API_SECRET", "").strip()
    if not key or not secret:
        sys.exit(f"{env}: BINANCE_API_KEY / BINANCE_API_SECRET missing")
    return ccxt.binance({"apiKey": key, "secret": secret, "enableRateLimit": True})


def check(env: str) -> int:
    ex = client(env)
    try:
        r = ex.sapiGetAccountApiRestrictions()
    except Exception as e:  # noqa: BLE001
        print(f"Binance refused the key: {type(e).__name__}: {str(e)[:200]}")
        print("Check that the key was copied in full and that its IP list contains this server (169.58.169.184).")
        return 1
    bad = [k for k in DANGEROUS if str(r.get(k)).lower() == "true"]
    print(
        f"reading: {r.get('enableReading')}; IP restricted: {r.get('ipRestrict')}; trading/transfer/withdraw on: {', '.join(bad) or 'none'}"
    )
    if str(r.get("enableReading")).lower() != "true":
        print("The key cannot read. Enable «Enable Reading» only.")
        return 1
    if bad:
        print("This key can do more than read. Create a new one with only «Enable Reading»; this one is not kept.")
        return 1
    if str(r.get("ipRestrict")).lower() != "true":
        print("Warning: no IP restriction. It works, but restrict it to 169.58.169.184 in Binance.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", default="/etc/tradedesk-binance.env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    a = ap.parse_args(argv)
    return check(a.env)


if __name__ == "__main__":
    sys.exit(main())
