# Нотатки про API (перевірені й неперевірені факти)

Позначки: **[V]** перевірено на реальній відповіді (2026-10-10, через DOUBLETOP `fetch`), **[D]** з документації/пам'яті, потребує живої перевірки (`make smoke`).

## Deribit (публічний REST) 
- База `https://www.deribit.com/api/v2`, JSON-RPC-обгортка `{jsonrpc,result|error,usIn,usOut,usDiff,testnet}` **[V]**.
- Ендпоїнти: `public/get_book_summary_by_currency?currency=BTC&kind=option|future`, `public/ticker?instrument_name=…`, `public/get_instruments`, `public/get_index_price`, `public/get_volatility_index_data` (DVOL) **[D]**.
- Ім'я інструмента: `BTC-11OCT26-73000-C` (день без нуля попереду) **[V]**. Експірація 08:00 UTC **[V]**. Перпетуал: `BTC-PERPETUAL`.
- Опціони інверсні: ціни в BTC; `open_interest` опціонів у BTC, ф'ючерсів у USD **[V]**.
- `mark_iv`/`bid_iv`/`ask_iv` — у **відсотках** (45.2 = 45.2 %) **[V]**.
- `underlying_price` у book summary/ticker — **форвард** експірації, не спот-індекс **[V]**.
- Ticker містить greeks Deribit (`delta,gamma,vega,theta,rho`): vega на 1 п.п. вол, theta на день, у USD-еквіваленті **[V]** → еталон для `option_calc` (звірка в `scripts/live_smoke.py`).
- Ліміти: консервативно 8 req/s, burst 10 **[D]**.
- Формула, що збіглася з Deribit: Black-76, r=0, T=дні/365, F=`underlying_price`; премія BTC × forward = Black-76 USD; `mark_price_usd` показується в UI через index.

## Hyperliquid
- `POST https://api.hyperliquid.xyz/info`, тіло `{"type": …}` **[V]** для `meta`, `fundingHistory`.
- `metaAndAssetCtxs` → `[meta, ctxs]`; `meta.universe[i] = {name, szDecimals, maxLeverage, onlyIsolated?, isDelisted?}` **[V]**; `ctxs[i]` (за порядком universe) `{funding, openInterest, prevDayPx, dayNtlVlm, premium, oraclePx, markPx, midPx, impactPxs}` — усі числа рядками **[D]**.
- Funding **щогодини**, значення — ставка за годину (дріб) **[V]**; annualize: ×24×365.
- `openInterest` — у базових одиницях (монетах), для USD помножити на mark **[D]**.
- `fundingHistory`: `{coin,fundingRate,premium,time}`, до 500 записів на запит **[V/D]**.
- `candleSnapshot`: `{"type":"candleSnapshot","req":{coin,interval,startTime,endTime}}`; ~5000 останніх свічок макс **[D]**.
- `l2Book`: `{levels:[[bids],[asks]]}` кожен рівень `{px,sz,n}` **[D]**.
- Ліміт ~1200 weight/хв на IP **[D]**; ми беремо 5 req/s.

## ccxt (async)
- Версія з `uv.lock`; клас `ccxt.async_support.<id>`; потрібно `await ex.close()` — наш `Exchanges.aclose()`.
- Уніфікований символ перпа лінійного USDT: `BTC/USDT:USDT` **[D]**.
- Id: `binanceusdm`, `bybit`, `okx` (для `okx` `fetch_funding_rate` / `fetch_open_interest`) **[D]**.
- Поля, що **потребують живої перевірки**: `fundingRate`, `nextFundingTimestamp`, `interval` (рядок на кшталт `8h` — у деяких версіях відсутній), `fetch_open_interest` → `openInterestAmount` / `openInterestValue`.
- Помилки: `RateLimitExceeded`, `DDoSProtection` → `rate_limited`; `AuthenticationError`, `PermissionDenied` → `auth`; `NetworkError`, `RequestTimeout`, `ExchangeNotAvailable` → `network`; `BadSymbol`, `BadRequest` → `data_unavailable`; решта `ExchangeError` → `backend_error`.

## Polymarket (планується)
- Gamma API `https://gamma-api.polymarket.com/markets?…` — метадані ринків, `outcomePrices`, `clobTokenIds`, `volume`, `liquidity` **[D]**.
- CLOB API `https://clob.polymarket.com`: `/price?token_id=&side=`, `/book?token_id=`, `/prices-history?market=<token>&interval=&fidelity=` **[D]**.
- Ціна «Yes» ≈ імплікована ймовірність; для багатоісходних ринків потрібен de-vig (`odds_devig`).

## Інші публічні джерела (план)
- Крипто Fear & Greed: `https://api.alternative.me/fng/?limit=N` **[D]**.
- Binance futures: `/futures/data/globalLongShortAccountRatio`, `/futures/data/openInterestHist`, `/fapi/v1/fundingRate` **[D]**.
- Bybit v5: `/v5/market/account-ratio`, `/v5/market/open-interest`, `/v5/market/funding/history` **[D]**.
- Liquidations: WebSocket Binance `!forceOrder@arr`, Bybit `allLiquidation.<symbol>`, OKX `liquidation-orders`, Hyperliquid немає прямого публічного фіда ліквідацій **[D]**.
- mempool.space, DefiLlama (`api.llama.fi`, `yields.llama.fi`, `stablecoins.llama.fi`), DexScreener, Etherscan V2, Pendle API (`api-v2.pendle.finance`) **[D]**.

## MCP SDK 2.x (`mcp==2.2.0`)
- `from mcp.server.mcpserver import MCPServer`; `server = MCPServer(name, instructions=…, lifespan=…)`.
- Декоратор `@server.tool(description=…, annotations=ToolAnnotations(readOnlyHint=True, …))`.
- Транспорти: `server.run("stdio")`, `server.run("streamable-http", host=, port=)`.
- Тест у процесі: `from mcp import Client; async with Client(server) as c: await c.call_tool(name, args)`.
- **Lifespan виконується на кожне з'єднання** (див. інваріант №7 у `CLAUDE.md`).

## Мережеві обмеження середовища розробки
Хмарний sandbox і Mac-воркспейс Claude: PyPI і GitHub доступні, біржі (Deribit, Binance, Bybit, OKX, Hyperliquid) — **403 на CONNECT**. Тому живу перевірку виконує власник на своїй машині або VPS.

## Підводні камені
- Різні одиниці IV (Deribit у %, ми — у дробах).
- OI опціонів у BTC vs ф'ючерсів у USD; OI Hyperliquid у монетах.
- Інтервал funding різний (1h/4h/8h): порівнювати лише після нормалізації до річного.
- Простий і складний (log) річний funding розходяться; ми віддаємо обидва.
- `fundingRate` Binance у `premiumIndex` — поточна оцінка, не виплачена.
- Експірації пʼятниці 08:00 UTC; квартальні ф'ючерси — остання пʼятниця кварталу.
