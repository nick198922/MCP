# Паритет із DOUBLETOP

DOUBLETOP: **74 інструменти** (66 від 17 бекендів + 8 власних `dt_edu`). Доступ планується відкликати, тому тут для кожного інструмента вказано, що буде замість нього. Оновлено 2026-10-17. Поки доступ є, агенти користуються ним для звірки й зразків (`docs/AGENTS.md`, «MCP DOUBLETOP»), але не з коду.

Легенда: ✅ замінено · 🔜 заплановано (фаза) · ⏭ свідомо не переносимо

## Власний шар `dt_edu` (8)

| DOUBLETOP | Рішення |
|---|---|
| `provider_readiness` | ✅ `system_health` (+ `probe=true` для живої перевірки; у DOUBLETOP 15 з 17 перевірок були застарілими) |
| `whoami` | ⏭ автентифікація буде нашою (Фаза 6) |
| `chain_trend_snapshot` (панелі TVL, DEX-обсяг, fees), `pair_discovery` | ✅ `chain_metrics` (DefiLlama: TVL, DEX-обсяг і fees за 24 год зі змінами 7д/30д та week-over-week, десяткові частки) і `dex_pairs` (DexScreener: числа числами, symbol без пробілів, сортування за ліквідністю, прапорець `likely_fake` для «SOL»-підробок). `trending_tokens` не відтворюємо: у DOUBLETOP це фейкові токени ([IDEA-20261017-doubletop-to-ours](ideas/IDEA-20261017-doubletop-to-ours.md)) |
| `token_card_snapshot`, `risk_hints_token`, `risk_hints_address`, `wallet_snapshot` | 🟡 `risk_hints_token` ≈ `contract_risk` (23.10: GoPlus token_security для 7 EVM-мереж + ліквідність / кап DexScreener; вердикти pass / fail / unknown за порогами курсу; Solana, honeypot.is і Etherscan — далі). Решта 🔜 Фаза 5. Перевірка 17.10: картка ETH порожня, ризики PEPE порожні (зразки в `tests/fixtures/doubletop/`); GoPlus 23.10 для PEPE теж дав порожній `result` |

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
| coinalyze `funding_rate_history` | ✅ `perps_funding_history` (з бірж); поточний — `perps_funding_compare`; довга історія Binance — ✅ `coinalyze_funding_history` (Coinalyze, `.A` = Binance; відсотки → десятковий дріб, звірено з виплатами Binance 2026-10-18 на зразку DoubleTop; живий виклик з нашим ключем ще не звірено) |
| coinalyze `open_interest_history` | ✅ `perps_oi_history` (з бірж, коротка історія); довга історія Binance — ✅ `coinalyze_oi_history` (Coinalyze, `.A` = Binance; `exchanges` — сума Binance `.A` + Bybit `.6`; USD (монети × close інтервалу) або монети; мітка — початок інтервалу; живий виклик ще не звірено) |
| coinalyze `liquidation_history` | ✅ живі — `liquidations_summary` (WebSocket-збирач); історія — ✅ `liquidations_history` (Coinalyze, Binance (суфікс `.A` = Binance, не агрегат; звірка 2026-10-18) або сума Binance `.A` + Bybit `.6` через `exchanges` (звірка `.6` 2026-10-25); USD = монети × close дня, daily/hourly; потрібен `COINALYZE_API_KEY`; живу відповідь ще не звірено; у DoubleTop денна історія є щонайменше з 2024-01) |
| coinalyze `long_short_ratio_history` | ✅ `perps_long_short_ratio` (Binance/Bybit); довга історія Binance — ✅ `coinalyze_long_short_history` (Coinalyze, `.A` = Binance, рахунки, частки 0–1; живий виклик ще не звірено) |
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
| defillama (7) | ✅ `chain_metrics` (TVL, DEX-обсяг, fees мережі); 🟡 `stablecoin_supply` (05.11: `stablecoins.llama.fi`, сума всіх стейблкоїнів і USDT / USDC у USD зі змінами 7д / 30д, десяткові частки; описовий, не сигнал; тест на синтетичному зразку документованої форми, форму й поля звірено живцем 05.11); потоки стейблкоїнів по мережах і пули — Фаза 5 |
| dexscreener (2) | ✅ `dex_pairs` ([IDEA-20261017-doubletop-to-ours](ideas/IDEA-20261017-doubletop-to-ours.md)) |
| etherscan (8) | 🔜 Etherscan V2 (безкоштовний ключ). `getTokenInfo` і `tokenTopHolders` — лише платний Pro |
| helius (5) | 🔜 безкоштовний тариф із ключем |
| mempool (5) | 🔜 публічний API mempool.space |
| pendle (6) | 🟡 `pendle_fixed_yields` (≈ `scan_fixed_yields`: фільтри за ліквідністю, APY, днями, stable, мережею) і `pendle_market` (один ринок + APY за ціною PT). 🟡 `pendle_yield_curve` (≈ `yield_curve`: точки за експірацією без інтерполяції, прапорець thin; паритет із DT не звірено — нема фікстури). 🟡 `pendle_expiry_calendar` (≈ `expiry_calendar`: групи за датою UTC, сумарна ліквідність/TVL; без `wallet`). ≈ `compare_pt_vs_underlying` покривається полем `implied_minus_underlying` у `pendle_fixed_yields`/`pendle_yield_curve`. 🔜 `liquidity_depth` (потрібне котирування свопу, інший ендпоінт). Корисно для funding-carry |

## Не переносимо

| DOUBLETOP | Причина |
|---|---|
| fetch `fetch` | ⏭ у Claude є власні веб-інструменти |
| time `get_current_time`, `convert_time` | ⏭ тривіально; натомість ✅ `calendar_expiries` (експірації опціонів Deribit daily / weekly / monthly / quarterly о 08:00 UTC, моменти виплат funding на біржах, макроподії FOMC / CPI / NFP зі статичного списку, що оновлюється вручну; огляд перед угодою, не прогноз) |
| blockscout, free-crypto-news | ⏭ у DOUBLETOP обидва зараз недоступні |

## Чого в DOUBLETOP не було, а в нас є або буде

Опціони (vol surface, skew, greeks, max pain, GEX, імпліковані ймовірності), basis і term structure, порівняння funding між майданчиками з нормалізацією інтервалів, slippage-калькулятор, квант-інструменти (de-vig, barrier-ймовірності), далі: пам'ять, алерти, ризик, правила, бектест, виконання.

Премія за волатильність: `deribit_vrp` (DVOL проти реалізованої, IV rank без сховища, хеджований продаж стреддла по рівнях DVOL) + бектест `scripts/verify_vrp.py`.

Funding carry: `funding_spread_scan` (крос-біржовий спред для перп-перп з прапорцями ризику; Hyperliquid опційно, kPEPE зіставляється з 1000PEPE за множником), `funding_carry_scan` (trailing APR з фактичних виплат, пороги, окупність комісій, наявність спота, фільтр TradFi-перпів) + бектест cash-and-carry (`scripts/verify_funding_carry.py`).

Паперова перевірка carry перп-перп: `carry_paper_open` / `carry_paper_status` / `carry_paper_close` (пара ніг на двох біржах у локальній SQLite, облік як у бектесті `simulate_perp_spread_portfolio`; закриті пари дзеркаляться в `journal` з тегом `pp-paper`). Ордерів немає.

Паперовий журнал рекомендацій асистента: `assistant_paper_open` / `assistant_paper_status` (структура з живого ланцюга Deribit за марками ± 1 вп, розмір за депозитом і ризиком, розрахунок на delivery; таблиця `rec_paper` з міткою `hypothesis`, у `journal` не дзеркалиться). Аналога в DOUBLETOP немає. Ордерів немає.

Журнал прогнозів асистента: `forecast_log_status` (щоденні ризик-нейтральні ймовірності чотирьох подій на delivery BTC і ETH з усмішки Deribit ≈ 7 днів; розрахунок на delivery, Brier, калібрування, таблиця за тижнями; модель-еталон RV30 і парне порівняння з нею; таблиця `forecast_log`, пише вимкнене за замовчуванням завдання колектора `forecast_log`). Аналога в DOUBLETOP немає. Ордерів немає.

Плани угод асистента: `plan_add` / `plan_list` / `plan_review` / `plan_link` (кожен план за будь-якого вердикту в таблиці `trade_plan`, оцінка на свічках біржі за правилами `analytics/trade_plan.py`, підсумок за вердиктом і тегом; щогодинна робота колектора `plan_resolve`). Аналога в DOUBLETOP немає. Ордерів немає.

Форвард-папір carry від рушія колектора: `paper_carry_status` (дві книги A `cc-paper` і B `pp-paper`, правила й облік спільні з бектестами в `analytics/carry_rules.py`; відкриває й закриває сам рушій, не людина). Аналога в DOUBLETOP немає. Ордерів немає.

Розриви Polymarket ↔ опціони: `polymarket_options_gap` (поточні розриви відкритих ринків BTC/ETH проти ймовірності з поверхні Deribit після комісії й спреду; колектор `pm_gap` пише щогодини для форвард-тесту CLV). Аналога в DOUBLETOP немає. Ордерів немає.

Потоки ринку (на кшталт панелі Coinglass): `market_flow` — спот-CVD і перп-CVD Binance (з поля taker buy свічок), OI Binance + Bybit у монетах і USD, funding (річний), ліквідації (Coinalyze), частка лонгів рахунків, премія Coinbase (сира і мінус медіана 30 днів), квадрант «ціна × OI × перп-CVD» за 4/24 год і описове речення українською. У DOUBLETOP були лише окремі ряди Coinalyze; комбінованого читання не було. Описові дані, не сигнал ([IDEA-20261028-market-flow-reader](ideas/IDEA-20261028-market-flow-reader.md)).

Режим циклу (на кшталт денної панелі BTC у каналі QG): `cycle_regime` — стан останнього закритого дня (дно / накопичення / перегрів / ріст / падіння) за зафіксованими до даних порогами з денних закриттів Binance: просадка від 365-денного максимуму, funding7 (річний), базис7 перп/спот, RSI14, ціна до SMA365 і нахил SMA365; найближчий інший стан і що має змінитись; історія станів. У DOUBLETOP аналога не було. Контекст, не сигнал; перевірку на історії ще не зроблено ([IDEA-20261028-btc-cycle-regime-gauge](ideas/IDEA-20261028-btc-cycle-regime-gauge.md)).

Price-action за навчальними відео DoubleTop (не з їхнього MCP): `range_detect` (рейнджі, sweeps, deviations, accumulation/distribution), `range_setup` (SFP-сетап: entry/stop/TP/RR), `range_setup_scan` (сканер SFP-сетапів по списку тикерів).

## Наші інструменти без відповідника

Повний перелік зареєстрованих інструментів, яких у DOUBLETOP немає (за каталогом [reference/doubletop-catalog.md](reference/doubletop-catalog.md)), тому перший стовпець — «—». Опис стислий, узятий з опису інструмента в коді (`src/tradedesk/tools/`); одиниці й застереження повністю — там. Звірка з реєстром: `uv run python scripts/docs_tools_check.py`.

### Опціони Deribit

| DOUBLETOP | Наш інструмент |
|---|---|
| — | ✅ `deribit_vol_surface` — поверхня волатильності BTC/ETH: на кожну експірацію форвард, ATM IV, risk reversal і butterfly 25Δ/10Δ, очікуваний рух 1σ, OI, put/call за OI; IV — десяткові; фільтри ліквідності до побудови усмішки, прапорець `thin_wing`. Відправна точка для опціонних питань |
| — | ✅ `deribit_option_chain` — ланцюг однієї експірації: по страйку call і put (IV, delta, gamma, vega на 1 пункт вола, theta за день у USD, mark у USD, OI, обсяг) у межах `width_pct` % від форварда |
| — | ✅ `deribit_atm_term` — терм-структура ATM IV: на кожну експірацію форвард, дні до експірації, ATM IV на форварді (лінійна інтерполяція в log-moneyness); крива вгору — контанго, вниз — бекуордація |
| — | ✅ `deribit_skew_term` — терм-структура скью: RR і butterfly на обраній дельті по експіраціях, найекстремальніша експірація за модулем RR, напрям `flattening` / `steepening` / `stable` |
| — | ✅ `deribit_expected_move` — очікуваний рух з ATM-стреддла: вартість стреддла в USD, беззбиткові F ± стреддл, діапазон з ATM IV × √T |
| — | ✅ `deribit_max_pain` — max pain однієї експірації (USD), відстань від форварда, put/call, п'ять страйків з найбільшим OI. Евристика, не прогноз: притягання розрахунку до цих рівнів на наших даних не підтверджено (BTC daily 2021–2026, n = 1 393) |
| — | ✅ `deribit_gamma_exposure` — модель GEX дилерів: нетто GEX у USD на 1 % руху, найбільші додатні / від'ємні страйки, рівень зміни знака. Евристика (припущення: дилери long calls / short puts); притягання розрахунку до рівнів не підтверджено |
| — | ✅ `deribit_oi_by_strike` — стіни OI: call / put OI по страйках у контрактах і USD, найбільші call- і put-стіни; знімок позиціонування, не прогноз |
| — | ✅ `deribit_largest_trades` — найбільші останні угоди опціонів або ф'ючерсів: напрям тейкера, обсяг, ціна, IV, нотіонал USD, блок-принти, підсумок (частка блоків, дисбаланс тейкерів); інформативно |
| — | ✅ `deribit_dvol` — ряд індексу DVOL (у відсотках річних: 45.3 = 45.3 %): OHLC, latest / mean / min / max / stdev, зміна за 24 год і 7 днів |
| — | ✅ `deribit_vrp` — премія за волатильність: DVOL проти реалізованої 7д / 30д, перцентиль DVOL за історією, історичний продаж 30-денного ATM-стреддла з денним дельта-хеджем за рівнями DVOL; історичне, не прогноз |
| — | ✅ `deribit_futures_curve` — терм-структура ф'ючерсів Deribit: перп і датовані з базисом до спот-індексу (абсолютний, %, річний простий і лог) |
| — | ✅ `iv_rank` — IV rank і перцентиль експірації за локальною історією роботи колектора ATM-term; потрібна збережена історія |
| — | ✅ `skew_percentile` — z-score і перцентиль RR 25Δ / 10Δ експірації за локальною історією роботи `atm_term` |
| — | ✅ `option_calc` — калькулятор Black-76 (r = 0, як у Deribit): ціна в USD і грецькі; vega на 1 пункт вола, theta за день |
| — | ✅ `prob_from_options` — ризик-нейтральна ймовірність рівня з опціонів Deribit: above / below на експірацію (Breeden-Litzenberger з урахуванням скью), touch до експірації; не прогноз реального світу |

### Перпи, funding, потоки

| DOUBLETOP | Наш інструмент |
|---|---|
| — | ✅ `perps_open_interest` — OI базового активу по біржах у USD, де доступно (ccxt-біржі, Hyperliquid, Deribit для BTC/ETH) |
| — | ✅ `perps_basis_compare` — крос-біржовий знімок базису (Binance, Bybit, OKX, Hyperliquid, Deribit): mark, спот-індекс, премія (bps і дріб), річний funding, плюс крива датованих ф'ючерсів Deribit |
| — | ✅ `funding_zscore` — z-score і перцентиль річного funding однієї біржі × базового активу за локальною історією роботи колектора funding |
| — | ✅ `funding_carry_scan` — funding carry на одній біржі: поточний і trailing APR з фактичних виплат, статус щодо порогів входу / виходу, дні окупності комісій, наявність спота; десяткові дроби; carry не безризиковий |
| — | ✅ `funding_spread_scan` — крос-біржовий спред funding для перп-перп carry: найкраща пара, trailing і поточний спред, витрати, прапорці ризику; Hyperliquid опційно (бектест HL ↔ Binance збитковий після витрат: моніторинг, не сигнал) |
| — | ✅ `market_flow` — потоки однієї монети: спот- і перп-CVD Binance, OI, funding, ліквідації (Coinalyze), частка лонгів рахунків, премія Coinbase, квадрант ціна × OI × перп-CVD. Окремо ці потоки в наших бектестах напрям не передбачили; описово, не сигнал |
| — | ✅ `cycle_regime` — денний режим циклу (дно / накопичення / перегрів / ріст / падіння) за зафіксованими порогами з денних даних Binance; контекст, не сигнал; на історії не перевірено |

### Прогнозні ринки, ймовірності, коефіцієнти

| DOUBLETOP | Наш інструмент |
|---|---|
| — | ✅ `polymarket_options_gap` — розриви між відкритими ринками Polymarket BTC/ETH і ризик-нейтральною ймовірністю тієї самої події з поверхні Deribit, після комісії тейкера й половини спреду; розрив — не доведена перевага |
| — | ✅ `touch_probability` — ймовірність дотику бар'єра за `days` при заданій IV (безперервний моніторинг, без дрейфу, стала вола, r = 0): замкнена формула і перевірка Монте-Карло; модельне значення, не прогноз |
| — | ✅ `odds_devig` — математика букмекера: маржа, сирі й справедливі ймовірності та коефіцієнти (методи `proportional` і `power`) |

### Календар подій

| DOUBLETOP | Наш інструмент |
|---|---|
| — | ✅ `events_add` / `events_list` / `events_seed_load` — локальний календар подій: додати подію (ідемпотентно), список із фільтрами за `kind`, `impact` і датами UTC, вбудований сід FOMC / CPI / NFP 2024–2027 і виборів США 2026-11-03 (дати ФРС 2026 у сіді прогнозні, звіряти з офіційними) |
| — | ✅ `event_study` — event-study: розподіл метрики у вікні навколо збережених подій (`direction`, `close_vs_t0`, `max_up`, `max_dd`, `volatility`), `n_sample`, прапорець малої вибірки при n < 20; описова статистика, не прогноз |

### Ціна: режим, рейнджі, рівні

| DOUBLETOP | Наш інструмент |
|---|---|
| — | ✅ `market_regime` — режим up / down / chop за нахилом і `trend_strength` на останніх барах (з `ohlcv_hist` або живі бари ccxt) |
| — | ✅ `range_detect` — рейнджі, sweeps і deviations на OHLCV (за лекцією DoubleTop «Ranges») |
| — | ✅ `range_setup` / `range_setup_scan` — SFP-сетап з останнього sweep у рейнджі (entry / stop / tp1 / tp2 / RR) і сканер таких сетапів по символах однієї біржі; механічна пропозиція, дворічний walk-forward значущої переваги не знайшов |
| — | ✅ `fib_retracement` — драбина Фібоначчі 23.6 / 38.2 / 50 / 61.8 / 78.6 % між ціною до реакції та її екстремумом, прапорець `applicable`; чиста математика, без даних |

### Стратегії та бектест

| DOUBLETOP | Наш інструмент |
|---|---|
| — | ✅ `strategy_payoff` — виплата багатоногої стратегії: крива, беззбиткові, max прибуток / збиток на сітці, нетто-грецькі Black-76, ризик-нейтральна ймовірність прибутку при одній волі (підказка, не прогноз) |
| — | ✅ `strategy_scenario_matrix` — матриця сценаріїв: переоцінка стратегії за Black-76 по горизонтах × цінах, P&L і нетто-грецькі |
| — | ✅ `strategy_straddle` / `strategy_strangle` / `strategy_iron_condor` / `strategy_risk_reversal` — шаблони опціонних структур: ноги плюс блок виплати / грецьких / PoP |
| — | ✅ `strategy_cash_and_carry` — long спот + short датований ф'ючерс; конвергенція базису не моделюється, PoP — лише перевірка |
| — | ✅ `strategy_funding_carry` — long перп на дешевій біржі + short на дорогій; показано лише конвергенцію mark-спреду, різницю funding не нараховує |
| — | ✅ `strategy_backtest_momentum` — walk-forward бектест моментуму на OHLCV ccxt з комісіями й прослизанням; перевірка, не пропозиція стратегії; без funding, один інструмент |

### Пам'ять: журнал, позиції, плани

| DOUBLETOP | Наш інструмент |
|---|---|
| — | ✅ `journal_add` / `journal_close` / `journal_list` / `journal_stats` — локальний журнал угод (SQLite): відкрити (стоп потрібен для R), закрити або скасувати, список із фільтрами, статистика закритих (win-rate, очікування в USD, R) |
| — | ✅ `positions_add` / `positions_close` / `positions_list` — локальний портфель, що ведеться вручну, з грецькими на контракт; закриття ордер не ставить |
| — | ✅ `positions_risk` — нетто-грецькі відкритого портфеля і сценарний P&L (наближення δ, γ, vega, θ; за замовчуванням ±5 / ±10 % спот, ±5 пунктів вола, крах із ростом вола) |
| — | ✅ `plan_add` / `plan_list` / `plan_review` / `plan_link` — плани угод асистента: запис за будь-якого вердикту, список, оцінка на свічках біржі (R з комісією тейкера), прив'язка до реальної угоди в `journal`; ордерів немає |

### Правила й алерти

| DOUBLETOP | Наш інструмент |
|---|---|
| — | ✅ `rules_add` / `rules_disable` / `rules_list` / `rules_test` — DSL-правила в локальному сховищі: створити, увімкнути / вимкнути / видалити, список, перевірити вираз без збереження |
| — | ✅ `rules_presets_load` — вбудовані пресети правил (екстремуми funding, z-score funding, HL ↔ Deribit carry, каскади ліквідацій); пороги не бектестовані й не з досліджень |
| — | ✅ `alerts_recent` — історія алертів з локальної SQLite; потрібна увімкнена робота alerts (`TRADEDESK_ALERTS_ENABLED=1`) |

### Паперові книги й журнал прогнозів

| DOUBLETOP | Наш інструмент |
|---|---|
| — | ✅ `carry_paper_open` / `carry_paper_status` / `carry_paper_close` — паперові пари перп-перп carry вручну, позначка живими даними, стоп як у бектесті, закриті пари дзеркаляться в `journal` (тег `pp-paper`); медіанна угода бектесту від'ємна; ордерів немає |
| — | ✅ `paper_carry_status` — стан двох форвард-паперових книг рушія колектора (A `cc-paper`, B `pp-paper`) за останнім 8-годинним циклом; живих даних не бере, ордерів немає |
| — | ✅ `assistant_paper_open` / `assistant_paper_status` — паперовий журнал опціонних рекомендацій асистента (ціни з живого ланцюга Deribit, розрахунок на delivery, мітка `hypothesis`, у `journal` не дзеркалиться); ордерів немає |
| — | ✅ `forecast_log_status` — журнал ризик-нейтральних прогнозів діапазону BTC/ETH: розрахунок на delivery, Brier, калібрування, порівняння з моделлю RV30 |
