#!/bin/bash
# From the Mac: listen to each liquidation WebSocket from the server for 45 s and print how many events arrived, plus the
# rows already in the store. A diagnostic that only reads.
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" 'cd ~/tradedesk-mcp && ~/.local/bin/uv run -q python -c "
import asyncio, sqlite3, time
from tradedesk.providers import liquidations as L
async def count(name, gen):
    n = 0
    try:
        async for _ in gen: n += 1
    except Exception as e:
        return f\"{name}: error {type(e).__name__}: {e}\"
    return f\"{name}: {n} events in 45 s\"
async def main():
    for r in await asyncio.gather(
        count(\"binance\", L.binance_liquidation_stream(max_seconds=45)),
        count(\"bybit\", L.bybit_liquidation_stream([\"BTCUSDT\", \"ETHUSDT\", \"SOLUSDT\"], max_seconds=45)),
        count(\"okx\", L.okx_liquidation_stream(\"SWAP\", max_seconds=45))):
        print(r)
asyncio.run(main())
con = sqlite3.connect(\"file:/var/lib/tradedesk-mcp/tradedesk.sqlite?mode=ro\", uri=True)
print(\"store:\", con.execute(\"SELECT venue, count(*), max(ts) FROM liquidations GROUP BY venue\").fetchall(), \"now\", int(time.time()))
"'
