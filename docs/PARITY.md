# Паритет із DOUBLETOP

DOUBLETOP: **74 інструменти** (66 від 17 бекендів + 8 власних `dt_edu`). Доступ планується відкликати, тому тут для кожного інструмента вказано, що буде замість нього.

Легенда: ✅ замінено · 🔜 заплановано (фаза) · ⏭ свідомо не переносимо

## Власний шар `dt_edu` (8)

| DOUBLETOP | Рішення |
|---|---|
| `provider_readiness` | ✅ `system_health` (+ `probe=true` для живої перевірки; у DOUBLETOP 15 з 17 перевірок були застарілими) |
| `whoami` | ⏭ автентифікація буде нашою (Фаза 6) |
| `token_card_snapshot`, `chain_trend_snapshot`, `pair_discovery`, `risk_hints_token`, `risk_hints_address`, `wallet_snapshot` | 🔜 Фаза 5: модуль ончейну, з тим самим `evidence`-контрактом. Низький пріоритет для деривативного трейдингу |

## Біржі та деривативи

| DOUBLETOP | Наш аналог |
|---|---|
| ccxt `get-ticker` | ✅ `market_ticker` |
| ccxt `get-ohlcv` | ✅ `market_ohlcv` (+ реалізована вола) |
| ccxt `get-orderbook` | ✅ `market_orderbook` (+ slippage під ваш розмір) |
| ccxt `batch-get-tickers` | ✅ `market_tickers` (сортування за quote volume, top-movers за 24h %) |
| ccxt `get-markets` | ✅ `market_markets` (пошук за підрядком, фільтр spot/swap/future/option, active) |
| ccxt `get-trades` | ✅ `market_trades` (+ aggressor flow: buy/sell notional, imbalance, VWAP, CVD, топ-прінти) |
| coinalyze `funding_rate_history` | 🔜 Фаза 1 (історія з бірж); поточний funding ✅ `perps_funding_compare` |
| coinalyze `open_interest_history` | 🔜 Фаза 2 (власний збирач; біржі дають коротку історію) |
| coinalyze `liquidation_history` | 🔜 Фаза 2 (WebSocket-збирач: історії ліквідацій в API бірж немає) |
| coinalyze `long_short_ratio_history` | 🔜 Фаза 1 (публічні ендпоїнти Binance/Bybit) |
| hyperliquid `meta_and_asset_ctxs` | ✅ `hyperliquid_overview` (у DOUBLETOP ця відповідь обрізається й контексти з funding/OI не повертаються) |
| hyperliquid `l2_book`, `candle_snapshot`, `funding_history`, `recent_trades` | 🔜 Фаза 1 |

## Ринок, сентимент, прогнозні ринки

| DOUBLETOP | Рішення |
|---|---|
| coingecko `execute`, `search_docs` | 🔜 Фаза 1: типізовані інструменти замість code-sandbox (у DOUBLETOP він періодично падає) |
| coinstats `coin_chart`, `coin_detail`, `coins_markets` | 🔜 Фаза 1 (покривається CoinGecko) |
| coinstats `market_insights` | 🔜 Фаза 1: домінація BTC, mcap |
| coinstats `wallet_balances` | 🔜 Фаза 5 |
| fear_greed `get_fear_greed_index` | ✅ `sentiment_fear_greed` — крипто-індекс alternative.me, 1..365 днів, latest + zone, mean 7d/30d, min/max, change_7d/30d, zone_share. Деннa частотa; шо DOUBLETOP відважив — індекс фондового ринку США, у нас виключно крипто |
| graph_polymarket (7) | 🔜 Фаза 1: Gamma + CLOB API (ціни, OI, історія), плюс звірка з options-implied. Топ-трейдери через subgraph потребують ключа The Graph |

## Ончейн, DeFi (Фаза 5, низький пріоритет)

| DOUBLETOP | Рішення |
|---|---|
| defillama (7) | 🔜 TVL, stablecoin flows, пули (публічний API) |
| dexscreener (2) | 🔜 публічний API |
| etherscan (8) | 🔜 Etherscan V2 (безкоштовний ключ). `getTokenInfo` і `tokenTopHolders` — лише платний Pro |
| helius (5) | 🔜 безкоштовний тариф із ключем |
| mempool (5) | 🔜 публічний API mempool.space |
| pendle (6) | 🔜 Pendle API + власна логіка (fixed yield, крива, PT vs underlying, календар експірацій, глибина). Корисно для funding-carry |

## Не переносимо

| DOUBLETOP | Причина |
|---|---|
| fetch `fetch` | ⏭ у Claude є власні веб-інструменти |
| time `get_current_time`, `convert_time` | ⏭ тривіально; натомість 🔜 календар експірацій і funding |
| blockscout, free-crypto-news | ⏭ у DOUBLETOP обидва зараз недоступні |

## Чого в DOUBLETOP не було, а в нас є або буде

Опціони (vol surface, skew, greeks, max pain, GEX, імпліковані ймовірності), basis і term structure, порівняння funding між майданчиками з нормалізацією інтервалів, slippage-калькулятор, квант-інструменти (de-vig, barrier-ймовірності), далі: пам'ять, алерти, ризик, правила, бектест, виконання.

Премія за волатильність: `deribit_vrp` (DVOL проти реалізованої, IV rank без сховища, хеджований продаж стреддла по рівнях DVOL) + бектест `scripts/verify_vrp.py`.

Funding carry: `funding_carry_scan` (trailing APR з фактичних виплат, пороги, окупність комісій, наявність спота, фільтр TradFi-перпів) + бектест cash-and-carry (`scripts/verify_funding_carry.py`).

Price-action за навчальними відео DoubleTop (не з їхнього MCP): `range_detect` (рейнджі, sweeps, deviations, accumulation/distribution), `range_setup` (SFP-сетап: entry/stop/TP/RR), `range_setup_scan` (сканер SFP-сетапів по списку тикерів).
