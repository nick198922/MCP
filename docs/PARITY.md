# Паритет із DOUBLETOP

DOUBLETOP: **74 інструменти** (66 від 17 бекендів + 8 власних `dt_edu`). Доступ планується відкликати, тому тут для кожного інструмента вказано, що буде замість нього. Оновлено 2026-10-17. Поки доступ є, агенти користуються ним для звірки й зразків (`docs/AGENTS.md`, «MCP DOUBLETOP»), але не з коду.

Легенда: ✅ замінено · 🔜 заплановано (фаза) · ⏭ свідомо не переносимо

## Власний шар `dt_edu` (8)

| DOUBLETOP | Рішення |
|---|---|
| `provider_readiness` | ✅ `system_health` (+ `probe=true` для живої перевірки; у DOUBLETOP 15 з 17 перевірок були застарілими) |
| `whoami` | ⏭ автентифікація буде нашою (Фаза 6) |
| `chain_trend_snapshot` (панелі TVL, DEX-обсяг, fees), `pair_discovery` | ✅ `chain_metrics` (DefiLlama: TVL, DEX-обсяг і fees за 24 год зі змінами 7д/30д та week-over-week, десяткові частки) і `dex_pairs` (DexScreener: числа числами, symbol без пробілів, сортування за ліквідністю, прапорець `likely_fake` для «SOL»-підробок). `trending_tokens` не відтворюємо: у DOUBLETOP це фейкові токени ([IDEA-20261017-doubletop-to-ours](ideas/IDEA-20261017-doubletop-to-ours.md)) |
| `token_card_snapshot`, `risk_hints_token`, `risk_hints_address`, `wallet_snapshot` | 🔜 Фаза 5. Перевірка 17.10: картка ETH порожня, ризики PEPE порожні (зразки в `tests/fixtures/doubletop/`) |

## Біржі та деривативи

Звірку чисел із DOUBLETOP для шести пар (OHLCV, OI, funding, long/short, Hyperliquid funding, Polymarket history) ще **не виконано**: очікувані розбіжності (одиниці, час, покриття) і що лишилося перевірити — у [reviews/2026-10-18-doubletop-parity.md](reviews/2026-10-18-doubletop-parity.md). Доти статус ✅ нижче означає «функціонал є», а не «числа збігаються».

| DOUBLETOP | Наш аналог |
|---|---|
| ccxt `get-ticker` | ✅ `market_ticker` |
| ccxt `get-ohlcv` | ✅ `market_ohlcv` (+ реалізована вола) |
| — (власне, курс DoubleTop) | ✅ `chart_markup` — розмітка графіка: EMA-групи, VWAP, профіль об'єму, структура курсу (HL/HH, BOS/CHoCH/sweep) і внутрішня, ліквідність, FVG, OB/брейкери, premium/discount/OTE, азійська сесія; працює й для форекс-перпів BingX |
| ccxt `get-orderbook` | ✅ `market_orderbook` (+ slippage під ваш розмір) |
| ccxt `batch-get-tickers` | ✅ `market_tickers` (сортування за quote volume, top-movers за 24h %) |
| ccxt `get-markets` | ✅ `market_markets` (пошук за підрядком, фільтр spot/swap/future/option, active) |
| ccxt `get-trades` | ✅ `market_trades` (+ aggressor flow: buy/sell notional, imbalance, VWAP, CVD, топ-прінти) |
| coinalyze `funding_rate_history` | ✅ `perps_funding_history` (з бірж); поточний — `perps_funding_compare` |
| coinalyze `open_interest_history` | ✅ `perps_oi_history` (з бірж, коротка історія); довга — 🔜 власний ключ Coinalyze |
| coinalyze `liquidation_history` | ✅ живі — `liquidations_summary` (WebSocket-збирач); історія — ✅ `liquidations_history` (Coinalyze, лише Binance (суфікс `.A` = Binance, не агрегат; звірка 2026-10-18), daily/hourly; потрібен `COINALYZE_API_KEY`; живу відповідь ще не звірено; у DoubleTop денна історія є щонайменше з 2024-01) |
| coinalyze `long_short_ratio_history` | ✅ `perps_long_short_ratio` (Binance/Bybit) |
| hyperliquid `meta_and_asset_ctxs` | ✅ `hyperliquid_overview` (у DOUBLETOP ця відповідь обрізається й контексти з funding/OI не повертаються) |
| hyperliquid `l2_book`, `candle_snapshot`, `funding_history`, `recent_trades` | ✅ `hyperliquid_book`, `hyperliquid_candles`, `hyperliquid_funding_history`, `hyperliquid_trades` |

## Ринок, сентимент, прогнозні ринки

| DOUBLETOP | Рішення |
|---|---|
| coingecko `execute`, `search_docs` | ✅ `coin_snapshot`, `top_coins`, `global_market` (у DOUBLETOP code-sandbox CoinGecko 17.10 знову падав) |
| coinstats `coin_chart`, `coin_detail`, `coins_markets` | ✅ покрито `coin_snapshot`, `top_coins`, `ohlcv_history` |
| coinstats `market_insights` | ✅ `global_market` |
| coinstats `wallet_balances` | 🔜 Фаза 5 |
| fear_greed `get_fear_greed_index` | ✅ `sentiment_fear_greed` — крипто-індекс alternative.me, 1..365 днів, latest + zone, mean 7d/30d, min/max, change_7d/30d, zone_share. Деннa частотa; шо DOUBLETOP відважив — індекс фондового ринку США, у нас виключно крипто |
| — (у DOUBLETOP макрокалендаря нема) | ✅ `macro_calendar` — ForexFactory, події з високим впливом (USD + ECB/BoJ/BoE), час UTC і Київ, прогноз/попереднє; дати поза вікном двох тижнів — із сіду (наближені) |
| graph_polymarket (7) | ✅ `polymarket_search`, `polymarket_market`, `polymarket_history`; топ-трейдери — колектор `trader_snapshot` (лідерборд Polymarket); OI-історія — 🔜 |

## Ончейн, DeFi (Фаза 5, низький пріоритет)

| DOUBLETOP | Рішення |
|---|---|
| defillama (7) | 🔜 `chain_metrics` (TVL, DEX-обсяг, fees мережі); stablecoin flows і пули — Фаза 5 |
| dexscreener (2) | 🔜 `dex_pairs` ([IDEA-20261017-doubletop-to-ours](ideas/IDEA-20261017-doubletop-to-ours.md)) |
| etherscan (8) | 🔜 Etherscan V2 (безкоштовний ключ). `getTokenInfo` і `tokenTopHolders` — лише платний Pro |
| helius (5) | 🔜 безкоштовний тариф із ключем |
| mempool (5) | 🔜 публічний API mempool.space |
| pendle (6) | 🟡 `pendle_fixed_yields` (≈ `scan_fixed_yields`: фільтри за ліквідністю, APY, днями, stable, мережею) і `pendle_market` (один ринок + APY за ціною PT). 🟡 `pendle_yield_curve` (≈ `yield_curve`: точки за експірацією без інтерполяції, прапорець thin; паритет із DT не звірено — нема фікстури). 🟡 `pendle_expiry_calendar` (≈ `expiry_calendar`: групи за датою UTC, сумарна ліквідність/TVL; без `wallet`). ≈ `compare_pt_vs_underlying` покривається полем `implied_minus_underlying` у `pendle_fixed_yields`/`pendle_yield_curve`. 🔜 `liquidity_depth` (потрібне котирування свопу, інший ендпоінт). Корисно для funding-carry |

## Не переносимо

| DOUBLETOP | Причина |
|---|---|
| fetch `fetch` | ⏭ у Claude є власні веб-інструменти |
| time `get_current_time`, `convert_time` | ⏭ тривіально; натомість 🔜 календар експірацій і funding |
| blockscout, free-crypto-news | ⏭ у DOUBLETOP обидва зараз недоступні |

## Чого в DOUBLETOP не було, а в нас є або буде

Опціони (vol surface, skew, greeks, max pain, GEX, імпліковані ймовірності), basis і term structure, порівняння funding між майданчиками з нормалізацією інтервалів, slippage-калькулятор, квант-інструменти (de-vig, barrier-ймовірності), далі: пам'ять, алерти, ризик, правила, бектест, виконання.

Премія за волатильність: `deribit_vrp` (DVOL проти реалізованої, IV rank без сховища, хеджований продаж стреддла по рівнях DVOL) + бектест `scripts/verify_vrp.py`.

Funding carry: `funding_spread_scan` (крос-біржовий спред для перп-перп з прапорцями ризику), `funding_carry_scan` (trailing APR з фактичних виплат, пороги, окупність комісій, наявність спота, фільтр TradFi-перпів) + бектест cash-and-carry (`scripts/verify_funding_carry.py`).

Паперова перевірка carry перп-перп: `carry_paper_open` / `carry_paper_status` / `carry_paper_close` (пара ніг на двох біржах у локальній SQLite, облік як у бектесті `simulate_perp_spread_portfolio`; закриті пари дзеркаляться в `journal` з тегом `pp-paper`). Ордерів немає.

Паперовий журнал рекомендацій асистента: `assistant_paper_open` / `assistant_paper_status` (структура з живого ланцюга Deribit за марками ± 1 вп, розмір за депозитом і ризиком, розрахунок на delivery; таблиця `rec_paper` з міткою `hypothesis`, у `journal` не дзеркалиться). Аналога в DOUBLETOP немає. Ордерів немає.

Форвард-папір carry від рушія колектора: `paper_carry_status` (дві книги A `cc-paper` і B `pp-paper`, правила й облік спільні з бектестами в `analytics/carry_rules.py`; відкриває й закриває сам рушій, не людина). Аналога в DOUBLETOP немає. Ордерів немає.

Розриви Polymarket ↔ опціони: `polymarket_options_gap` (поточні розриви відкритих ринків BTC/ETH проти ймовірності з поверхні Deribit після комісії й спреду; колектор `pm_gap` пише щогодини для форвард-тесту CLV). Аналога в DOUBLETOP немає. Ордерів немає.

Price-action за навчальними відео DoubleTop (не з їхнього MCP): `range_detect` (рейнджі, sweeps, deviations, accumulation/distribution), `range_setup` (SFP-сетап: entry/stop/TP/RR), `range_setup_scan` (сканер SFP-сетапів по списку тикерів).
