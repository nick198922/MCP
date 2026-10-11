# BACKLOG

Формат: `[ ]` — не зроблено, `[x]` — зроблено. Позначка **(verify)** — ендпоїнт/поле з документації, потребує живої перевірки (див. `docs/reference/api-notes.md`). Нумерація стабільна: на неї посилаються інші документи.

## Фаза 0 — ядро ✅
- [x] Http (retry/rate-limit/cache/health), Envelope, провайдери Deribit/HL/ccxt, 16 інструментів, 51 тест.

## Фаза 0.5 — гігієна (робити першими)
- [x] 0.1 `make smoke` виконано 2026-10-10, 9/9 ok; виправлено OKX order-book (3+ колонки); зафіксовано, що Binance/Bybit не повертають `openInterestValue` (окремий пункт нижче).
- [x] 0.2 `ruff` у dev-extras, `uv lock`.
- [x] 0.3 Python 3.11 перевірено (51 тест).
- [x] 0.4 Жорсткі шляхи з README прибрано.
- [x] 0.5 CI активний і зелений: три останні прогони на 3.11/3.12/3.13 — success (`78e0e71`, `b24a050`, `04e10fe`).
- [x] 0.6 `perps_open_interest`: fallback `oi_usd = openInterestAmount × last` для Binance/Bybit (через `ccxt.fetch_ticker`), з warning. Якщо ticker теж недоступний — лишається `None`.
- [x] 0.7 `scripts/live_smoke.py` розширено: `market_tickers`, `market_markets`, `market_trades`, `deribit_expected_move`, `deribit_oi_by_strike` (14 кейсів, порог 10/14).

## Фаза 1 — дані та деривативи (залишок)
- [x] 1.1 `market_tickers` (batch), `market_markets` (пошук/фільтр, не повний дамп), `market_trades` (+ агресор-дисбаланс/CVD). ccxt `fetch_tickers`, `fetch_markets`, `fetch_trades`.
- [x] 1.2 `perps_funding_history`: ccxt `fetch_funding_rate_history` (Binance/Bybit/OKX, 8h) + HL `fundingHistory` (1h); z-score, перцентиль, mean 7d/30d, carry-APR, polarity flips. Крос-венюз-summary (highest/lowest carry, biggest |z|). Live-verified 2026-10-10 на реальних даних. TODO: пагінація HL після 500 записів.
- [x] 1.3 `perps_long_short_ratio`, `perps_oi_history`: Binance `/futures/data/globalLongShortAccountRatio`, `/futures/data/openInterestHist`; Bybit `/v5/market/account-ratio`, `/v5/market/open-interest`. Нові провайдери `binance_futures.py`, `bybit.py`. Live-verified 2026-10-10. Bybit OI без USD-value (так само як 0.6 — потрібен fallback через mark price).
- [x] 1.4 Hyperliquid: `hyperliquid_candles` (OHLCV + realized vol), `hyperliquid_book` (+slippage, 20 рівнів), `hyperliquid_trades` (aggressor flow), `hyperliquid_funding_history` (+ 7d/30d mean, z, percentile, carry). Шейпи ctxs і решти ендпойнтів підтверджено живим запитом 2026-10-10.
- [x] 1.5 Polymarket (частково): провайдер `polymarket.py` з двома `Http` (Gamma + CLOB), analytics `normalise_market`, `devig_multi`, `book_stats`; інструменти `polymarket_search`, `polymarket_market` (per-outcome CLOB mid/spread/depth), `polymarket_history`. **(verify)** Live-перевірка — під час наступного `make smoke`. `polymarket_vs_options` відкладено (треба маніфест mapping Polymarket-ринок → крипто-опціонна подія).
- [x] 1.6 `sentiment_fear_greed` (alternative.me, крипто, денна частота): latest + zone, mean 7d/30d, min/max, зміни 7d/30d, zone_share. Провайдер `alternative_me.py` (rate 2 req/s). Live-перевірка — під час наступного `make smoke` (кейс додано).
- [x] 1.7 CoinGecko типізовано: `coin_snapshot(coin_id, vs_currency)`, `global_market()`, `top_coins(limit, vs_currency, page)`. Опційний ключ (`COINGECKO_API_KEY` + `COINGECKO_PLAN=demo|pro`): rate 0.5 req/s без ключа, 5 req/s з ключем; header `x-cg-demo-api-key` або `x-cg-pro-api-key` відповідно.
- [x] 1.8 `calendar_expiries(currencies, horizon_days, funding_hours_ahead)`: Deribit експірації по валютах (`classify_expiry` → daily/weekly/monthly/quarterly), funding-schedule для binanceusdm/bybit/okx (8h) + HL (1h), макро-події з `src/tradedesk/data/macro_events.json` (FOMC/CPI/NFP, оновлюється вручну). Включено до wheel force-include.
- [x] 1.9 Волатильність (частково): `deribit_dvol(currency, hours_back, resolution)` — DVOL time series + stats + 24h/7d point-changes. `deribit_atm_term(currency, min_days, max_days)` — ATM-IV term structure зі shape (contango/backwardation/flat). IV rank/percentile і VRP залишаються до Фази 2 (історія). **(verify)** Live-перевірка — під час наступного `make smoke`.
- [ ] 1.10 Якість smile: фільтр ✅ (`min_oi`, `max_spread_pct`), прапорці `thin_wing_25d`/`thin_wing_10d` ✅; ліквідні ваги в LS-фіті smile — лишилися.
- [x] 1.11 Skew term structure: `deribit_skew_term(currency, target_delta, min_days, max_days)` — RR і fly по експіраціях (на будь-який call-equivalent delta у (0, 0.5)), найбільш екстремальна за |RR|, shape `flattening`/`steepening`/`stable`. `skew_percentile` — після Фази 2 (треба історія).
- [x] 1.12 Deribit: `deribit_largest_trades(currency, kind, count, only_blocks, min_notional_usd, top_n)` — топ трейдів за USD-notional, фільтр блок-угод, summary (buy/sell notional, taker imbalance, block share). `deribit_oi_by_strike` ✅, `deribit_expected_move` ✅.
- [x] 1.13 `perps_basis_compare(base)` — крос-венюз perp premium (mark/index) у bps + Deribit dated-futures basis annualized (simple і log); summary з highest/lowest premium, крос-венюзний spread (bps) і найближчим dated-contract. CME-проксі відкладено (потребує платної підписки); Deribit квартальних достатньо.

## Фаза 2 — пам'ять, збирач, алерти, деплой
Мета: сервер «працює, поки сплю».
- [x] 2.1 `store/` MVP: SQLite (WAL, PRAGMA user_version), `aiosqlite`. Таблиці: `atm_term(ts, currency, expiry, days, forward, atm_iv, rr25, fly25, rr10, fly10)`, `funding_hist(ts, venue, base, rate_per_interval, interval_hours, annualized)`, `basis_hist(ts, currency, expiry, days, basis_pct, annualized_simple)`, `snapshots(id, ts, kind, payload)`. Міграції через `MIGRATIONS` dict + `SCHEMA_VERSION`. Залишилось для пізніх кроків: `iv_surface` (per-strike), `oi`, `liquidations`, `alerts`, `journal`, `positions`.
- [x] 2.2 Збирач: `collector/scheduler.py` з `Scheduler`/`PeriodicJob`/`JobMetrics`; 4 дефолтні джоби — `atm_term` (5 хв), `funding` (1 год), `basis` (5 хв), `retention` (24 год). Увімкнути: `TRADEDESK_COLLECTOR_ENABLED=1`. Метрики в `system_health.data.collector`. Retention — simple `DELETE WHERE ts < cutoff` per-table з окремими windows (`TRADEDESK_RETENTION_{ATM_TERM,FUNDING,BASIS}_DAYS`, за замовчуванням 365). Downsampling лишається (простий retention достатній поки історія мала).
- [x] 2.3 Ліквідації: schema v5 + 3 WS-джерела. Binance `!forceOrder@arr`, Bybit v5 `allLiquidation.<symbol>` (список символів у `TRADEDESK_COLLECT_LIQUIDATIONS_BYBIT_SYMBOLS`, default `BTCUSDT,ETHUSDT,SOLUSDT`), OKX v5 `liquidation-orders` (`instType` у `TRADEDESK_COLLECT_LIQUIDATIONS_OKX_INST_TYPE`, default `SWAP`). Вибір venues через `TRADEDESK_COLLECT_LIQUIDATIONS_VENUES=binance,bybit,okx`. Загальний `_consume_liquidation_stream` драйнить потік у Store (batch 100). MCP-інструмент `liquidations_summary(window_minutes, venue?, symbol?)` — total/longs/shorts USD, imbalance, per-symbol, top. Live-перевірено 2026-10-11 (OKX STRK-USDT-SWAP ліквідації залетіли).
- [x] 2.4 Історичні інструменти поверх сховища: `iv_rank(currency, expiry, lookback_days)`, `funding_zscore(venue, base, lookback_days)` (поверх `funding_hist.annualized`), `skew_percentile(currency, expiry, delta_target='25'|'10', lookback_days)` (поверх `atm_term.rr25`/`rr10`). Усі повертають `latest/mean/stdev/min/max/z_score/percentile/rank`. `iv_percentile` фактично всередині `iv_rank` (окремий tool не потрібен).
- [x] 2.5 Алерти (MVP): `alerts/{engine,channels}.py`, провайдер `telegram.py` з `sendMessage`, нова таблиця `alerts(id, rule_id, ts, payload, channel, delivered, delivery_error)`. Простий DSL у JSON-файлі (metric: `funding`/`atm_iv`/`basis`; op: `> < >= <= ==`), per-rule `cooldown_seconds`. Канали: `log` (завжди) і `telegram` (опційно, якщо задані `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID`). Job `alerts` у scheduler-і (`TRADEDESK_ALERTS_ENABLED=1`, period `TRADEDESK_ALERTS_SECONDS`=60с). MCP-інструмент `alerts_recent(limit)` показує журнал. Повноцінний DSL із expr-мовою — у 3.1. Email/ntfy — пізніше.
- [x] 2.6 Журнал угод: таблиця `journal` (schema v3) + 4 MCP-tools. `journal_add(symbol, side, size, entry, stop?, target?, thesis?, tags?)`, `journal_list(status, symbol, tag, limit)`, `journal_close(id, exit?, cancelled?, note?)` (обчислює PnL = sign·size·(exit−entry) і R-multiple), `journal_stats(symbol?, tag?)` (win-rate, expectancy, avg/median/min/max R, total PnL). Чисті функції `analytics/journal.py::{pnl,r_multiple,outcome_of,summary}`. Пишуть у локальну SQLite; поза ExecutionGuard, read-only для бірж.
- [x] 2.7 Позиції й ризик: schema v4 (`positions`) + 4 MCP-tools. `positions_add(instrument, kind, side, size, entry?, mark?, delta?, gamma?, vega?, theta?, tags?, note?, opened_ts?)`, `positions_list(status?, instrument?, tag?)`, `positions_close(id, note?)`, `positions_risk(instrument_filter?, tag?, reference_spot?, scenarios?, days?)` з net δ/γ/vega/θ, per-instrument розподілом і сценаріями (default 7: ±5%/±10% spot, ±5 vol-points, crash+vol-up). Чиста аналітика `analytics/portfolio.py::{aggregate_greeks, scenario_pnl}`. Sync з біржі (read-only) — пізніше (у Фазі 4, читати позиції з ccxt/Deribit). Margin-estimate — тебпер відкладено (кожна біржа має свою формулу).
- [x] 2.8 Деплой (частково): `Dockerfile` (uv builder → slim runtime, non-root uid 10001, `sqlite3` CLI, HEALTHCHECK по TCP:8000), `docker-compose.yml` (bind 127.0.0.1, volume `./data:/data`), `deploy/tradedesk-mcp.service` (systemd + hardening), `deploy/backup-sqlite.sh` (`.backup` + gzip + ретенція `BACKUP_KEEP_DAYS`), `deploy/env.example`, `deploy/README.md` (Docker + bare-systemd варіанти + Caddy приклад). `/healthz` — чекає розширення MCP SDK (нотатка в deploy README). Off-site копіювання лишається вручну (`rclone`/`restic` поза скоупом).
- [x] 2.9 HTTP-auth: `BearerAuthMiddleware` для streamable-http, `TRADEDESK_HTTP_BEARER_TOKEN` (або `TRADEDESK_HTTP_BEARER_TOKENS` для списку). constant-time compare (`hmac.compare_digest`), 401 + `WWW-Authenticate: Bearer realm="tradedesk"`. Порожнє значення = auth вимкнена + WARNING у лозі. TLS досі зовні (Caddy/Nginx). Без ключа — лише 127.0.0.1/VPN.
- [ ] 2.10 Структуровані логи (JSON), ротація, метрики Prometheus (опційно).

## Фаза 3 — правила, стратегії, бектест
- [x] 3.1 DSL правил: безпечний AST-евалюатор (`src/tradedesk/rules/dsl.py`): цілочислова/float-арифметика, порівняння, bool-оператори, виклики лише із whitelist, заборонено Attribute/Subscript/Lambda/Comprehension/Walrus/dunder-імена. Metric functions (`src/tradedesk/rules/metrics.py`): `funding/funding_mean/funding_zscore`, `atm_iv/atm_iv_mean/atm_iv_zscore`, `rr25`, `basis/basis_annualized`, `liquidations_total/imbalance` + `abs/min/max`. Schema v6: `rules(id, name, enabled, expr, message, cooldown_seconds, created_ts, disabled_ts)`. 4 MCP-tools: `rules_add`, `rules_list(enabled_only?)`, `rules_test(expr?)` (парсить + eval + повертає value, truthy, або list of metrics коли без expr), `rules_disable(id, enable?, delete?)`. `alerts` job тепер читає й JSON (Phase 2.5), і DB-DSL — rule_id у таблиці alerts стає `"db:<name>"`. Cooldown — через `last_alert_ts(rule_id)`. Повноцінні комбінатори all/any доступні через `and`/`or` у вираженні.
- [ ] 3.2 Бібліотека сигналів: funding-екстремуми, basis-розширення, skew-флип, GEX-flip-наближення, IV-rank, OI+price дивергенції, liquidation-каскади, Polymarket-vs-options розбіжність.
- [ ] 3.3 Стратегії-шаблони (опис + параметри + умови входу/виходу): cash-and-carry, funding-арбітраж між майданчиками, calendar spread, risk reversal, iron condor/short strangle з дельта-хеджем, вола-карри (VRP). Кожна — з калькулятором payoff і ризик-профілем.
- [x] 3.4 Payoff/сценарії: `strategy_payoff(legs, spot, days, iv, forward?, width_pct=0.3, grid=201, kind='detailed'|'summary')`. Чиста аналітика `analytics/strategy.py`: `payoff_curve(legs, prices)`, `aggregate_greeks(legs, forward, days, iv)` (Black-76 на forward), `mtm_value`, `breakevens(prices, pnl)` (лінійна інтерполяція zero-crossings), `prob_of_profit(legs, prices, pnl, forward, days, iv)` (log-normal CDF інтегрована по profit-сегментах, визначених через breakevens). Legs: `{kind: 'call'|'put'|'linear', side, size, strike?, entry_premium?, entry?}`. Повертає криву PnL, max profit/loss на grid, net greeks, MTM, PoP, breakevens. `kind='summary'` викидає криву з відповіді. Для surface-based IV у кожній нозі — пізніше (поки один `iv`).
- [ ] 3.5 Бектест на власній історії: векторизований рушій, комісії/слиппедж/funding, walk-forward, звіт (Sharpe, maxDD, tail). Попередження про дрібну історію.
- [ ] 3.6 Режими ринку (вола/тренд) як контекст для правил.

## Фаза 4 — виконання ордерів (поетапно)
Вимоги: `docs/SECURITY.md`, `docs/adr/0005`.
- [ ] 4.1 Пакет `execution/`: `ExecutionGuard` (ліміти розміру, notional, плечі, символів-allowlist, денний max-loss, kill-switch, режим paper/testnet/live), аудит-лог (append-only).
- [ ] 4.2 Paper-режим: симуляція fill по стакану; журнал і P&L.
- [ ] 4.3 Двокроковий потік: `order_propose` → повертає `proposal_id` + повний розрахунок (ціна, slippage, margin, ризик) → `order_confirm(proposal_id)` з TTL; повторний запуск неможливий (idempotency key).
- [ ] 4.4 Testnet: Deribit testnet, Binance/Bybit testnet через ccxt `set_sandbox_mode`. Окремі ключі.
- [ ] 4.5 Live, мінімальні ліміти, ключі без права виведення + IP allow-list; перші ордери вручну разом із власником.
- [ ] 4.6 Керування: `order_cancel`, `order_amend`, `positions_close_all` (аварійне), автоматичні стопи/OCO там, де біржа підтримує.
- [ ] 4.7 Дельта-хедж для опціонних позицій (пропозиції, не автовиконання, на старті).

## Фаза 5 — IBKR та ончейн
- [ ] 5.1 IBKR через `ib_async`: підключення до IB Gateway (на VPS: IBC + Xvfb або Docker `gnzsnz/ib-gateway`), market-data (потрібна підписка), ланцюги опціонів, ф'ючерси (ES/NQ/CL/GC), greeks від IBKR; потім execution через той самий `ExecutionGuard`.
- [ ] 5.2 Єдиний шар інструментів (`instrument_id`, мультибіржові символи), нормалізація ф'ючерсних контрактів (серії, ролл).
- [ ] 5.3 Ончейн-паритет з DOUBLETOP: DefiLlama, DexScreener, Etherscan V2, Helius, mempool, `token_card`/`wallet_snapshot` з тим самим `evidence`-контрактом.
- [ ] 5.4 Pendle (fixed yield, крива, PT vs underlying, календар) — для funding-carry стратегій.

## Фаза 6 — безпека та полірування
- [ ] 6.1 Багатокористувацька автентифікація (токени з ролями read/trade), rate-limit по клієнту.
- [ ] 6.2 Секрети: sops/age або Docker secrets; ротація ключів.
- [ ] 6.3 Підписані релізи, Dependabot, SBOM.
- [ ] 6.4 Документація користувача: цикли використання («ранковий брифінг», «пре-трейд чеклист»), MCP-промпти (`@server.prompt`) для типових сценаріїв.

## Ідеї без пріоритету
- MCP-ресурси (`resource://`) для статичних довідників (специфікації контрактів, календар).
- MCP-промпти: `morning_briefing`, `pre_trade_checklist`, `post_mortem` (базовий `token_deep_analysis` ✅).
- Експорт у Google Sheets/CSV.
- Сповіщення про зміну health провайдерів.
