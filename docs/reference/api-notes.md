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
- `metaAndAssetCtxs` → `[meta, ctxs]`; `meta.universe[i] = {name, szDecimals, maxLeverage, onlyIsolated?, isDelisted?, marginTableId}` **[V]**; `ctxs[i]` (за порядком universe) `{funding, openInterest, prevDayPx, dayNtlVlm, premium, oraclePx, markPx, midPx, impactPxs, dayBaseVlm}` — усі числа рядками **[V]** (2026-10-10, 234 монети).
- Funding **щогодини**, значення — ставка за годину (дріб) **[V]**; annualize: ×24×365.
- `openInterest` — у базових одиницях (монетах), для USD помножити на mark **[D]**.
- `fundingHistory`: `{coin,fundingRate,premium,time}`, до 500 записів на запит **[V/D]**.
- `candleSnapshot`: `{"type":"candleSnapshot","req":{coin,interval,startTime,endTime}}`; свічка `{t,T,s,i,o,h,l,c,v,n}` з числами **рядками** **[V]**; ~5000 останніх свічок макс **[D]**.
- `l2Book`: `{coin,time,levels:[[bids],[asks]]}` кожен рівень `{px,sz,n}` як рядок — 20 рівнів на сторону **[V]**.
- `recentTrades`: `{coin,side:"B"|"A",px,sz,time,hash,tid,users}` **[V]**; "B" = taker buy (lifted ask), "A" = taker sell. Повертає до 500 трейдів, у низько-активний момент може бути значно менше (бачили 10).
- Ліміт ~1200 weight/хв на IP **[D]**; ми беремо 5 req/s.

## ccxt (async)
- Версія з `uv.lock`; клас `ccxt.async_support.<id>`; потрібно `await ex.close()` — наш `Exchanges.aclose()`.
- Уніфікований символ перпа лінійного USDT: `BTC/USDT:USDT` **[D]**.
- Id: `binanceusdm`, `bybit`, `okx` (для `okx` `fetch_funding_rate` / `fetch_open_interest`) **[D]**.
- Помилки: `RateLimitExceeded`, `DDoSProtection` → `rate_limited`; `AuthenticationError`, `PermissionDenied` → `auth`; `NetworkError`, `RequestTimeout`, `ExchangeNotAvailable` → `network`; `BadSymbol`, `BadRequest` → `data_unavailable`; решта `ExchangeError` → `backend_error`.

### Живі зразки (snapshot 2026-10-10, BTC/USDT:USDT) **[V]**
`fetch_funding_rate`:
- Binance (`binanceusdm`): `interval=null` → наш fallback 8h + warning (`parse_interval_hours` → 8); `nextFundingTimestamp=null`, але `fundingTimestamp` є.
- Bybit: `interval="8h"` ✓, `nextFundingTimestamp=null`, `fundingTimestamp` є.
- OKX: `interval="8h"` ✓, і `nextFundingTimestamp`, і `fundingTimestamp`; **`markPrice`/`indexPrice=null`** (не повертає).

`fetch_funding_rate_history` (всі 3): уніфіковано `{symbol, fundingRate, timestamp, info}`. Інтервал у відповіді відсутній — ми детектимо з медіани ts-різниць (`detect_interval_hours`); на BTC виходить 8h для всіх 3 ccxt-venues.

`fetch_open_interest`:
- Binance: `openInterestAmount` (BTC) є, **`openInterestValue=null`**.
- Bybit: `openInterestAmount` є, **`openInterestValue=null`**.
- OKX: обидва є (`openInterestValue` у USD).
- Наслідок: `perps_open_interest` для Binance/Bybit повертає `oi_usd=None` + warning. **Можлива покращення (backlog):** додати ще один виклик, щоб узяти mark (з `fetch_ticker` або `fetch_funding_rate`) і множити на amount.

### OKX order book — додаткові колонки **[V]**
Рівні у `fetch_order_book` на OKX виглядають як `[price, size, 0, orders_count]` (4 елементи). Наш `book_stats`/`walk_book` беруть `level[0]`, `level[1]` — толерантні. Тест: `tests/test_analytics.py::test_book_tolerates_extra_level_columns`.

## Polymarket (планується)
- Gamma API `https://gamma-api.polymarket.com/markets?…` — метадані ринків, `outcomePrices`, `clobTokenIds`, `volume`, `liquidity` **[D]**.
- CLOB API `https://clob.polymarket.com`: `/price?token_id=&side=`, `/book?token_id=`, `/prices-history?market=<token>&interval=&fidelity=` **[D]**.
- Ціна «Yes» ≈ імплікована ймовірність; для багатоісходних ринків потрібен de-vig (`odds_devig`).

## alternative.me (крипто Fear & Greed) **[V]**
- `GET https://api.alternative.me/fng/?limit=N&format=json` → `{name, data: [{value, value_classification, timestamp, time_until_update?}], metadata}`. `value` і `timestamp` — рядки; `time_until_update` присутній лише в найсвіжішому рядку. Денна частота, оновлення близько 00:00 UTC. Класифікація: `Extreme Fear` (0–24) / `Fear` (25–44) / `Neutral` (45–54) / `Greed` (55–74) / `Extreme Greed` (75–100).
- Живий snapshot `tests/fixtures/alternative_me_fng_live_2026-10-10.json` (30 рядків, 2026-10-10 ~14:40 UTC): останнє значення 64 (Greed), 7 днів тому 73 (Greed), тижневий тренд легка просадка.

## Інші публічні джерела (план)
- Binance futures: база `https://fapi.binance.com`; `/futures/data/globalLongShortAccountRatio` → `[{symbol, longAccount, shortAccount, longShortRatio, timestamp}]` всі рядки, longAccount+shortAccount≈1.0 (account fractions, не notional-weighted) **[V]**. `/futures/data/openInterestHist` → `[{symbol, sumOpenInterest, sumOpenInterestValue, CMCCirculatingSupply, timestamp}]`, OI у монетах і USD **[V]**. Періоди: `5m/15m/30m/1h/2h/4h/6h/12h/1d`, limit до 500 **[V]**.
- Bybit v5: база `https://api.bybit.com`; відповідь загорнута в `{retCode, retMsg, result: {list: [...]}}`. `/v5/market/account-ratio?category=linear` → `{symbol, buyRatio, sellRatio, timestamp}`, `buyRatio+sellRatio≈1.0` **[V]**. `/v5/market/open-interest?category=linear&intervalTime=1h` → `{openInterest, singleOpenInterest, timestamp}`, тільки в base-asset (USD-value **не повертається**) **[V]**. Періоди: `5min/15min/30min/1h/4h/1d`, limit до 500 **[V]**. `/v5/market/funding/history` **[D]**.
- Liquidations: WebSocket Binance `!forceOrder@arr`, Bybit `allLiquidation.<symbol>`, OKX `liquidation-orders`, Hyperliquid немає прямого публічного фіда ліквідацій **[D]**.
- mempool.space, DefiLlama (`api.llama.fi`, `yields.llama.fi`, `stablecoins.llama.fi`), DexScreener, Etherscan V2, Pendle API (`api-v2.pendle.finance`) **[D]**.

## MCP SDK 2.x (`mcp==2.2.0`)
- `from mcp.server.mcpserver import MCPServer`; `server = MCPServer(name, instructions=…, lifespan=…)`.
- Декоратор `@server.tool(description=…, annotations=ToolAnnotations(readOnlyHint=True, …))`.
- Транспорти: `server.run("stdio")`, `server.run("streamable-http", host=, port=)`.
- Тест у процесі: `from mcp import Client; async with Client(server) as c: await c.call_tool(name, args)`.
- **Lifespan виконується на кожне з'єднання** (див. інваріант №7 у `CLAUDE.md`).

## Мережеві обмеження середовища розробки
- **Хмарний sandbox Claude:** PyPI і GitHub доступні, біржі — **403 на CONNECT**.
- **Локальний Claude Code (Mac власника):** sandbox блокує `curl`, але `uv run python -m httpx`/ccxt виклики проходять. Тому `make smoke` і живі snapshot-и можна виконувати прямо з сесії Claude Code (підтверджено 2026-10-10 для Bybit, Hyperliquid, Binance, OKX, Deribit). На VPS обмежень не буде.

## Підводні камені
- Різні одиниці IV (Deribit у %, ми — у дробах).
- OI опціонів у BTC vs ф'ючерсів у USD; OI Hyperliquid у монетах.
- Інтервал funding різний (1h/4h/8h): порівнювати лише після нормалізації до річного.
- Простий і складний (log) річний funding розходяться; ми віддаємо обидва.
- `fundingRate` Binance у `premiumIndex` — поточна оцінка, не виплачена.
- Експірації пʼятниці 08:00 UTC; квартальні ф'ючерси — остання пʼятниця кварталу.
