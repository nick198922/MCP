#!/bin/bash
# From the Mac: can the server fetch forex data? Dukascopy history (scripts/fx_history.py, one day into /tmp) and
# BingX TradFi perps through ccxt (one EURUSD candle). Reads only; prints the result or the reason.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd ~/tradedesk-mcp && d=$(mktemp -d) \
  && ~/.local/bin/uv run -q python scripts/fx_history.py EURUSD --start 2026-10-14 --end 2026-10-14 --tf 1d --cache "$d" --out "$d/x.csv" 2>&1 | tail -2 \
  && tail -1 "$d/x.csv"; rm -r "$d"
  ~/.local/bin/uv run -q python -c "
import asyncio, ccxt.async_support as c
async def m():
    e = c.bingx({\"timeout\": 20000})
    try:
        print(\"bingx\", (await e.fetch_ohlcv(\"NCFXEUR2USD/USDT:USDT\", \"1h\", limit=2))[-1])
    finally:
        await e.close()
asyncio.run(m())" 2>&1 | tail -2'
