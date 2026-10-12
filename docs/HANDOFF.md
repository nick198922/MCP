# HANDOFF — стан проєкту

Оновлено: **2026-10-10** (sess. 3). Версія коду: **0.1.0 + unreleased**. Оновлюйте цей файл наприкінці кожної сесії.

## Мета і контекст
Власник (Микола) будує власний MCP-сервер для торгівлі ф'ючерсами й опціонами, бо доступ до чужого MCP **DOUBLETOP** (хаб із 74 інструментів) скоро закриють. Вимоги, які він озвучив:
- ринки: крипто-перпи/ф'ючерси; крипто-опціони (Deribit); Polymarket. **IBKR відкладено** на невизначений час (див. ADR 0006);
- мова: Python;
- сервер має **працювати й оновлюватись, поки власник спить** (не залежить від його ноутбука);
- «усі можливості, включно з виконанням ордерів, але поетапно»;
- розробка в цьому (хмарному) середовищі через Claude; власник планує працювати з терміналу, **файли не на його комп'ютері**.

Припущення, які ще не підтверджено власником (не вважати рішеннями): алерти через Telegram; хостинг на VPS.

## Що зроблено (Фаза 0 + Фаза 1 без 1.10 + 1.14-1.16 + Фаза 2 без 2.10 + Фаза 3 крім 3.2)
74 інструменти, публічні дані (CoinGecko вільно без ключа, з ключем — вищий ліміт):

| Група | Інструменти |
|---|---|
| Система | `system_health(probe)` |
| Ринок (ccxt) | `market_ticker`, `market_tickers`, `market_ohlcv`, `market_orderbook`, `market_markets`, `market_trades` |
| Перпи | `perps_funding_compare`, `perps_funding_history`, `perps_long_short_ratio`, `perps_oi_history`, `perps_open_interest`, `perps_basis_compare` |
| Hyperliquid | `hyperliquid_overview`, `hyperliquid_candles`, `hyperliquid_book`, `hyperliquid_trades`, `hyperliquid_funding_history` |
| Ф'ючерси | `deribit_futures_curve` |
| Опціони | `deribit_vol_surface`, `deribit_option_chain`, `deribit_max_pain`, `deribit_gamma_exposure`, `deribit_expected_move`, `deribit_oi_by_strike`, `deribit_dvol`, `deribit_atm_term`, `deribit_skew_term`, `deribit_largest_trades`, `prob_from_options` |
| Квант | `odds_devig`, `option_calc`, `touch_probability`, `fib_retracement` |
| Настрій | `sentiment_fear_greed` |
| Календар | `calendar_expiries` |
| Події (Фаза 1) | `events_add`, `events_list`, `events_seed_load`, `event_study` |
| Prediction markets | `polymarket_search`, `polymarket_market`, `polymarket_history` |
| Макро/ринок | `coin_snapshot`, `global_market`, `top_coins` |
| Історія (Фаза 2) | `iv_rank`, `funding_zscore`, `skew_percentile`, `alerts_recent`, `ohlcv_history`, `market_regime` |
| Журнал (Фаза 2) | `journal_add`, `journal_list`, `journal_close`, `journal_stats` |
| Позиції (Фаза 2) | `positions_add`, `positions_list`, `positions_close`, `positions_risk` |
| Ліквідації (Фаза 2) | `liquidations_summary` |
| Правила (Фаза 3) | `rules_add`, `rules_list`, `rules_test`, `rules_disable`, `rules_presets_load` |
| Стратегії (Фаза 3) | `strategy_payoff`, `strategy_straddle`, `strategy_strangle`, `strategy_risk_reversal`, `strategy_iron_condor`, `strategy_cash_and_carry`, `strategy_funding_carry`, `strategy_scenario_matrix` |
| Бектест (Фаза 3) | `strategy_backtest_momentum` |

Ядро: `Http` (ретраї на 429/5xx/timeout, token-bucket, TTL-кеш із single-flight, класифікація помилок), `HealthRegistry`, `Envelope/Evidence`, фейкові транспорти для тестів.

## Як перевірено
- **70 тестів, усі зелені** (локально; Python 3.15 через `uv`). Раніше проходили на 3.13/3.14.
- Промт `token_deep_analysis` зареєстрований і рендериться через MCP Client API (`list_prompts` + `get_prompt`).
- `ruff check` і `ruff format` чисті.
- Black-76 звірено з **реальним тікером Deribit** (delta −0.4535, gamma ≈2e-5, vega 226.95, theta −25.98) і з ціною (mark у BTC = USD-ціна / forward).
- Touch-ймовірності звірено з Monte Carlo (±1.5 %).
- Сервер запускається в обох режимах: stdio-підпроцес і streamable-http (перевірено клієнтом MCP).
- Зразки реальних відповідей Deribit (книга опціонів, ф'ючерси, ticker, instruments) і Hyperliquid (meta, funding history) отримані 2026-10-10 через DOUBLETOP `fetch` і лежать у `tests/fixtures/` та `docs/reference/api-notes.md`.

## Що зроблено у сесії 3 (2026-10-10)
- **0.5 закрито:** CI активний і зелений (`38072914168` — success на 3.11/3.12/3.13, ~18с кожна матриця). Попередні прогони `b24a050`/`78e0e71` також success.
- **0.6 закрито:** `perps_open_interest` тепер робить fallback `oi_usd = openInterestAmount × last` для бірж без `openInterestValue` (Binance/Bybit). Додатковий виклик `fetch_ticker` кешується TTL=2s. Якщо ticker недоступний — `None` + warning як раніше.
- **0.7 закрито:** `scripts/live_smoke.py` розширено до 15 кейсів (`market_tickers`, `market_markets`, `market_trades`, `deribit_expected_move`, `deribit_oi_by_strike`, `sentiment_fear_greed`). Поріг успіху 11/15.
- **1.6 закрито:** `sentiment_fear_greed(days=1..365)` — крипто Fear & Greed з alternative.me. Новий провайдер `alternative_me.py`, аналітика `summarise_fear_greed`. Повертає latest + zone (extreme_fear/fear/neutral/greed/extreme_greed), mean 7d/30d, min/max, change_7d/30d, zone_share, series.
- **1.8 закрито:** `calendar_expiries(currencies, horizon_days, funding_hours_ahead)` — три секції: Deribit expiries (daily/weekly/monthly/quarterly), funding payouts (Binance/Bybit/OKX 8h, HL hourly), macro events із `data/macro_events.json` (FOMC/CPI/NFP). Файл JSON підключено до wheel через `force-include`.
- **1.9 закрито (частково):** `deribit_dvol(currency, hours_back, resolution)` — DVOL OHLC-ряд, latest/mean/stdev/min/max і 24h/7d point-changes. `deribit_atm_term(currency)` — ATM-IV term structure з shape (contango/backwardation/flat). IV rank і VRP — до Фази 2 (історія в SQLite).
- **1.11 закрито:** `deribit_skew_term(currency, target_delta=0.25, min_days, max_days)` — per-expiry RR і fly, найбільш екстремальна за |RR|, крос-експірний `rr_shape` (flattening/steepening/stable). `skew_percentile` — до Фази 2.
- **1.12 закрито:** `deribit_largest_trades(currency, kind, count, only_blocks, min_notional_usd, top_n)` — топ-N трейдів за USD-notional, фільтр блок-угод, summary з taker imbalance.
- **1.13 закрито:** `perps_basis_compare(base)` — крос-венюз perp premium (bps) по Binance/Bybit/OKX/Hyperliquid/Deribit + Deribit dated-futures basis (simple і log річні). Summary: highest/lowest premium, крос-венюз spread, найближчий dated-contract.
- **1.5 закрито (частково):** Polymarket провайдер (Gamma + CLOB через два окремих `Http`), analytics (normalise_market, devig_multi, book_stats), 3 інструменти: `polymarket_search`, `polymarket_market` (з CLOB-мідами по outcome-ах), `polymarket_history`. `polymarket_vs_options` — пізніше, бо треба Polymarket→crypto-option mapping.
- **1.7 закрито:** CoinGecko провайдер з опційним ключем (env `COINGECKO_API_KEY` + `COINGECKO_PLAN`, demo/pro); 3 інструменти — `coin_snapshot`, `global_market`, `top_coins`. Rate 0.5 req/s без ключа.
- **Фаза 2 (MVP, початок):** 2.1 Store — SQLite (WAL, aiosqlite, `PRAGMA user_version`), 4 таблиці (`atm_term`, `funding_hist`, `basis_hist`, `snapshots`). 2.4 — перший історичний інструмент `iv_rank(currency, expiry, lookback_days)`.
- **Фаза 2.2 (повно):** scheduler із 4 дефолтними джобами — `atm_term` (5 хв), `funding` (1 год, крос-венюз snapshot у `funding_hist`), `basis` (5 хв, Deribit dated у `basis_hist`), `retention` (24 год, DELETE по таблицях за `TRADEDESK_RETENTION_*_DAYS`). Увімкнути: `TRADEDESK_COLLECTOR_ENABLED=1`; метрики в `system_health`.
- **Фаза 2.4 (повно):** `iv_rank(currency, expiry, lookback_days)`, `funding_zscore(venue, base, lookback_days)`, `skew_percentile(currency, expiry, delta_target='25'|'10', lookback_days)`. Усі повертають latest/mean/stdev/min/max + z_score + percentile + rank.
- **Фаза 2.8 (частково):** артефакти деплою — `Dockerfile`, `docker-compose.yml`, `deploy/{env.example,tradedesk-mcp.service,backup-sqlite.sh,README.md}`. Два шляхи — Docker Compose (рекомендовано) і bare systemd. Build-smoke та live-прогін на VPS робить власник (Docker на цій машині не встановлено).
- **Фаза 2.9:** HTTP bearer-auth для streamable-http через `BearerAuthMiddleware`. `TRADEDESK_HTTP_BEARER_TOKEN` (або `TRADEDESK_HTTP_BEARER_TOKENS` зі списком через кому). Порожнє значення → auth вимкнена + WARNING у лозі (запускати лише локально).
- **Фаза 2.5 (MVP):** rules → channels → store. Нова таблиця `alerts` (schema v2). Пакет `alerts/{engine,channels}.py`, провайдер `telegram.py`, scheduler-job `alerts` (увімкнути `TRADEDESK_ALERTS_ENABLED=1` + вказати `TRADEDESK_ALERTS_RULES_PATH`). Канали: `log` (завжди) + `telegram` (коли задані `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID`). MCP-інструмент `alerts_recent`. Повноцінний DSL — на 3.1.
- **Фаза 2.6:** журнал угод (schema v3, таблиця `journal`). Чиста аналітика `analytics/journal.py` (`pnl`, `r_multiple`, `outcome_of`, `summary`). Чотири MCP-tools: `journal_add`, `journal_list`, `journal_close` (з обчисленням PnL і R-multiple), `journal_stats` (win-rate, expectancy, R-stats). Локальна SQLite — ніяких ордерів назовні.
- **Фаза 2.7:** позиції й ризик (schema v4, таблиця `positions`). Чиста аналітика `analytics/portfolio.py` (`aggregate_greeks`, `scenario_pnl`). Чотири MCP-tools: `positions_add`, `positions_list`, `positions_close`, `positions_risk` з net δ/γ/vega/θ, per-instrument розподілом і 7 default-сценаріями (±5%/±10% spot, ±5 vol-points, крах+вол-ап). Sync з біржі (read-only) — у Фазі 4; margin-estimate — також пізніше.
- **Фаза 3.1:** DSL правил. Безпечний AST-евалюатор (`rules/dsl.py`) + metric functions (`rules/metrics.py`) + schema v6 (`rules`) + 4 MCP-tools (`rules_add/list/test/disable`). `alerts` job'и тепер читають і JSON (2.5), і DB-DSL (3.1). Доступні функції: `funding/funding_mean/funding_zscore`, `atm_iv/atm_iv_mean/atm_iv_zscore`, `rr25`, `basis/basis_annualized`, `liquidations_total/imbalance`, `abs/min/max`.
- **Фаза 3.4:** `strategy_payoff(legs, spot, days, iv, forward?, width_pct, grid, kind)` — many-leg payoff, breakevens, max profit/loss, net Black-76 δ/γ/vega/θ, MTM, risk-neutral PoP. Чиста аналітика `analytics/strategy.py`.
- **Фаза 3.3:** 6 шаблонів стратегій на базі pure-builders — `strategy_straddle`, `strategy_strangle`, `strategy_risk_reversal`, `strategy_iron_condor`, `strategy_cash_and_carry`, `strategy_funding_carry`. Кожен генерує legs і повертає payoff у єдиному форматі з `strategy_payoff`.
- **Фаза 3.5 (MVP):** векторизований бектест `analytics/backtest.py` (next-bar fill, costs за зміну позиції, узгоджені equity+returns) + сигнал-шаблон momentum. MCP-tool `strategy_backtest_momentum` поверх ccxt OHLCV. Live-перевірено на BTC/USDT 1h × 30d. Залишилось у Фазі 3: 3.2 (бібліотека пресет-правил), 3.6 (режими ринку), funding/multi-asset у бектесті, walk-forward splits.
- **Фаза 1.14 (нова, після DoubleTop FOMC-прикладу):** historical OHLCV store (schema v7). Collector-job `ohlcv_hist` з backfill і incremental pull; MCP-інструмент `ohlcv_history`. Фундамент для майбутніх `event_study` і `market_regime` (1.15, 1.16, 3.6).
- **Фаза 1.15:** schema v8 (`events`) + bundled seed JSON (FOMC/CPI/NFP 2024-2026, 96 подій, 2026 Fed-дати projected) + 3 MCP-tools (`events_add`, `events_list`, `events_seed_load`). Idempotency через `UNIQUE(ts, kind, label)`.
- **Фаза 1.16:** event-study engine `analytics/event_study.py` (5 measures: direction/close_vs_t0/max_up/max_dd/volatility; `small_sample_flag` коли n<20) + MCP-tool `event_study` поверх `ohlcv_hist` + `events`. Готовий фундамент для FOMC-style playbook'ів (DoubleTop pattern).
- **Фаза 3.7:** `strategy_scenario_matrix` — multi-horizon MTM-grid для стратегії. Black-76 на кожному горизонті, flat IV per horizon (feed різні IV у `horizons` для term-structure). Default schedule `[D/3, D/2, D]` і grid `[-10,-5,-2,0,2,5,10]%`.
- **Фаза 3.8:** `fib_retracement` — pure math ladder 23.6/38.2/50/61.8/78.6% з direction і noise-filter (`min_drop_pct`). Analytics `retracement.py`.
- **Фаза 3.6:** `market_regime` — up/down/chop класифікатор (OLS slope на log-closes, trend_strength = `|slope|·√n/vol`). Pure `analytics/regime.py`; MCP-tool із source=store/live. Live BTC 2026-10-12: 1h=down(0.89), 4h=chop(0.26), 1d=up(1.95).
- **Фаза 6.4:** три нових MCP-промпти — `fomc_playbook`, `pre_trade_checklist`, `morning_briefing`. Шаблони у `src/tradedesk/prompts/*.md`, запаковані у wheel. DoubleTop-паритет досягнуто: тепер MCP-клієнт має готові сценарії, що викликають усі наші analytics tools.
- **Фаза 3.2 (MVP):** bundled `rules_presets.json` з 8 готовими DSL-правилами + MCP-tool `rules_presets_load(only?, enable_disabled?)`. Один виклик додає весь сигнальний набір у локальний store.
- **Фаза 2.3:** schema v5 (`liquidations`) + WS-колектор для 3 venues. Binance `!forceOrder@arr` (без subscribe), Bybit v5 `allLiquidation.<symbol>` (per-symbol subscribe), OKX v5 `liquidation-orders` (один `instType`). Вибір venues через `TRADEDESK_COLLECT_LIQUIDATIONS_VENUES`. Загальний drain `_consume_liquidation_stream` → `insert_liquidations` батчами по 100. MCP-інструмент `liquidations_summary(window_minutes, venue?, symbol?)`. Live-перевірено (OKX STRK-USDT-SWAP ліквідації залетіли; Bybit підключився). Залишилось у Фазі 2: email/ntfy канали, off-site бекапи, `/healthz`, Prometheus (2.10).
- Тестів: 144 (+74 за сесію), lint і format чисті. Нові залежності: `aiosqlite`, `websockets`.
- **`make smoke` live 2026-10-10: 15/15 ok** на реальних біржах. Deribit greeks звірено з їхнім ticker (30OCT26, delta/vega/theta до 4 знаків). `perps_open_interest` з новим fallback відпрацював 669 мс. `sentiment_fear_greed` повернув 30 денних значень; живий зразок збережено в `tests/fixtures/alternative_me_fng_live_2026-10-10.json`.

## Що перевірено живими запитами 2026-10-10
Виявилось, що з сесії Claude Code на Mac власника мережа до бірж **працює** (через `uv run python … httpx/ccxt`; `curl` блокує sandbox). `make smoke` виконано, 9/9 інструментів ok. Деталі в `docs/reference/api-notes.md` (розділ «Живі зразки»):
- **Deribit greeks** збігаються з їхнім ticker до 4 знаків на delta/vega/theta (три BTC-кол біля ATM, 30OCT26).
- **ccxt funding `interval`**: Bybit і OKX повертають `"8h"`; Binance — `null` (наш fallback 8h + warning уже працює).
- **ccxt `openInterestValue`**: лише OKX. Binance/Bybit повертають `null` → `oi_usd=None` + warning. Залишається як покращення (взяти mark звідкись і множити на amount).
- **Hyperliquid `metaAndAssetCtxs`**: усі очікувані поля на місці (`funding/openInterest/markPx/oraclePx/midPx/impactPxs/dayBaseVlm/premium/dayNtlVlm/prevDayPx`), universe 234 монети.
- **OKX order book**: рівні з 4 колонок (включно з `orders_count`) → виправлено в `analytics/orderbook.py` + тест `test_book_tolerates_extra_level_columns`.

## Що ще НЕ перевірено
- `perps_open_interest` для Bybit: fallback у 0.6 покриває Binance; Bybit у ccxt `fetch_ticker` зазвичай повертає `last`, але не гарантовано. Жива перевірка — під час наступного `make smoke`.

## Ключові рішення (деталі в `docs/adr/`)
| ADR | Рішення |
|---|---|
| 0001 | Python + офіційний MCP SDK 2.x (`mcp.server.mcpserver.MCPServer`) |
| 0002 | Єдиний конверт відповіді `{ok,data,evidence,missing,warnings,as_of}` і graceful degradation |
| 0003 | Black-76, r=0, форвард експірації, IV у десяткових дробах |
| 0004 | Один довгоживучий asyncio-процес + SQLite; деплой на постійно ввімкнену машину (Фаза 2) |
| 0005 | Read-only за замовчуванням; ордери лише через ExecutionGuard, двокроковий потік |

## Відомі обмеження коду
- Funding-інтервал для ccxt береться з відповіді, інакше припускається 8h (з `warning`).
- Smile має необов'язкові фільтри `min_oi`/`max_spread_pct` (увімкніть у `deribit_vol_surface`), прапорці `thin_wing_25d/10d`. Вагові LS-фіти за ліквідністю досі не реалізовані.
- `prob_from_options(kind="touch")` використовує одну сталу IV (на рівні бар'єра).
- GEX припускає «дилери лонг коли, шорт путів» — евристика.
- Опціони лише BTC/ETH (інверсні) на Deribit; USDC-опціони на альти не підтримані.
- Кеш у пам'яті процесу; історії немає (з'являється у Фазі 2).
- HTTP-режим без автентифікації.
- Ліміт Deribit (8 req/s, burst 10) і Hyperliquid (5 req/s) — консервативні припущення.

## Збереження (відкрите питання №1)
Хмарне середовище тимчасове (знищується після неактивності). **Постійне сховище — приватний репозиторій GitHub.** На 2026-10-10 репозиторій ще не створено; локальний git у `/home/claude/tradedesk-mcp` має коміти. Власник створить порожній приватний репозиторій і назве `власник/назва`; тоді: підключити (`add_repo`, push) і `git push`. Працювати з терміналу можна з клону репозиторію.

## Відкриті питання до власника
1. ✅ GitHub-репозиторій `github.com/nick198922/MCP` (CI зелений на 3.11/3.12/3.13).
2. Хостинг для режиму «працює, поки сплю»: VPS (рекомендовано) — Hetzner CX11/22 чи DO; треба підтвердити конкретний.
3. Telegram-бот: створити через @BotFather, отримати `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` і покласти в `.env` на сервері — тоді `TelegramChannel` увімкнеться автоматично.
4. ~~IBKR~~ — **відкладено** за рішенням власника (див. ADR 0006).
5. Live-пробіг колектора: пройти `make smoke` з увімкненим collector'ом на кілька днів, щоб `iv_rank`/`skew_percentile`/`funding_zscore`/`event_study` набрали історію.

## Наступні кроки
Див. `docs/BACKLOG.md`. Фази 0-3 (крім 1.10, 2.10, 3.2) закриті. Основні гілки з того, що залишилось:
- **Фаза 4 (execution)** — `ExecutionGuard`, paper → testnet → live. Крипто тільки (без IBKR). Це серйозний блок, вимагає ADR на деталі.
- **Фаза 5.3** — ончейн-паритет (DefiLlama, DexScreener, Etherscan V2, Helius, mempool).
- **Фаза 5.4** — Pendle (fixed yield, крива).
- Малі хвости: 1.10, 2.10, 3.2.
