"""How fast may this machine read Deribit's public REST before HTTP 429 `too_many_requests` (code 10028)?

Read-only probe for docs/changes/2026-11-25-deribit-collector-gaps.md. It reads the BTC option book summary once, picks
the instruments the `option_xvenue` collector reads (nearest daily expiry 2-20 h away, ATM +- 3 strikes, calls and puts,
nearest the forward first), then fires those `public/ticker` requests the way the collector does (one `asyncio.gather`
paced by a token bucket) once per pace setting, with a pause between settings. Every request goes through
`core.http.Http` with retries=0, so each 429 is counted as Deribit sent it. Public data only, no key, no orders.

    uv run python scripts/probe_deribit_pace.py [--paces 8:10,4:4,2:2] [--pause 15] [--currency BTC]

Mind the shared IP: the live collector reads Deribit from the same machine, so run it away from the option_xvenue
hourly snapshot (``SELECT MAX(ts) FROM option_xvenue_snapshot WHERE mode='hourly'`` + 1 h) and keep it short.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter
from datetime import UTC, datetime

from tradedesk.analytics.option_xvenue import atm_strikes, pick_daily_expiry
from tradedesk.collector.option_xvenue import _deribit_chain
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.deribit import BASE_URL, Deribit


def _client(rate: float, burst: int) -> Deribit:
    return Deribit(Http("deribit", BASE_URL, rate=rate, burst=burst, retries=0, timeout=10.0))


async def _names(cur: str) -> list[str]:
    d = _client(1, 1)
    try:
        summary = (await d.book_summary(cur, "option")).data
    finally:
        await d.http.aclose()
    chain, fwd = _deribit_chain(summary, cur)
    expiry = pick_daily_expiry(chain, datetime.now(UTC))
    if expiry is None or expiry not in fwd:
        return []
    ks = atm_strikes(chain[expiry], fwd[expiry])
    names = [(k, chain[expiry][k][t]) for k in ks for t in ("C", "P") if t in chain[expiry][k]]
    names.sort(key=lambda x: abs(x[0] - fwd[expiry]))
    return [n for _, n in names]


async def _burst(names: list[str], rate: float, burst: int) -> list[tuple[str, float]]:
    """(outcome, seconds after the first request) per ticker; outcome 'ok' or the ProviderError reason."""
    d = _client(rate, burst)
    t0 = time.perf_counter()

    async def one(n: str) -> tuple[str, float]:
        try:
            await d.ticker(n)
            return "ok", time.perf_counter() - t0
        except ProviderError as e:
            return e.reason, time.perf_counter() - t0

    try:
        return list(await asyncio.gather(*(one(n) for n in names)))
    finally:
        await d.http.aclose()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--paces", default="8:10,4:4,2:2", help="rate:burst per setting, comma separated (8:10 = Runtime's before 2026-11-25)")
    ap.add_argument("--pause", type=float, default=15.0, help="seconds between settings")
    ap.add_argument("--currency", default="BTC")
    a = ap.parse_args()
    names = await _names(a.currency)
    print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC, {a.currency}: {len(names)} tickers per setting")
    if not names:
        return
    for i, pace in enumerate(a.paces.split(",")):
        if i:
            await asyncio.sleep(a.pause)
        rate, burst = pace.split(":")
        got = await _burst(names, float(rate), int(burst))
        counts = Counter(o for o, _ in got)
        first_bad = min((s for o, s in got if o != "ok"), default=None)
        seq = "".join("." if o == "ok" else ("R" if o == "rate_limited" else "x") for o, _ in sorted(got, key=lambda g: g[1]))
        print(
            f"rate {rate}/s burst {burst}: {dict(counts)}; span {max(s for _, s in got):.2f} s; "
            f"first failure at {first_bad if first_bad is None else round(first_bad, 2)} s; order {seq} (. ok, R 429)"
        )


if __name__ == "__main__":
    asyncio.run(main())
