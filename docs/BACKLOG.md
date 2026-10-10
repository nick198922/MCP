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
- [ ] 1.5 Polymarket: провайдер Gamma + CLOB; інструменти `polymarket_search`, `polymarket_market` (ціни Yes/No, спред, ліквідність), `polymarket_history`; `polymarket_vs_options` — порівняння з `prob_from_options` (touch/above) для крипто-ринків. De-vig для мульти-исходів. **(verify)**
- [x] 1.6 `sentiment_fear_greed` (alternative.me, крипто, денна частота): latest + zone, mean 7d/30d, min/max, зміни 7d/30d, zone_share. Провайдер `alternative_me.py` (rate 2 req/s). Live-перевірка — під час наступного `make smoke` (кейс додано).
- [ ] 1.7 CoinGecko типізовано: `coin_snapshot` (price, mcap, FDV, supply, ATH), `global_market` (домінація, mcap), `top_coins`. Безкоштовний tier + опційний ключ.
- [x] 1.8 `calendar_expiries(currencies, horizon_days, funding_hours_ahead)`: Deribit експірації по валютах (`classify_expiry` → daily/weekly/monthly/quarterly), funding-schedule для binanceusdm/bybit/okx (8h) + HL (1h), макро-події з `src/tradedesk/data/macro_events.json` (FOMC/CPI/NFP, оновлюється вручну). Включено до wheel force-include.
- [ ] 1.9 Волатильність: `deribit_dvol` (`public/get_volatility_index_data`), IV rank/percentile (потребує історії → Фаза 2), VRP = IV − реалізована, term structure ATM IV по експірації. **(verify)**
- [ ] 1.10 Якість smile: фільтр ✅ (`min_oi`, `max_spread_pct`), прапорці `thin_wing_25d`/`thin_wing_10d` ✅; ліквідні ваги в LS-фіті smile — лишилися.
- [ ] 1.11 Skew-метрики: 25Δ-RR і butterfly по всіх експірціях (термова структура skew), `skew_percentile` (після Фази 2).
- [ ] 1.12 Deribit: `deribit_largest_trades` (блок-угоди `public/get_last_trades_by_currency`); `deribit_oi_by_strike` ✅, `deribit_expected_move` ✅.
- [ ] 1.13 Cross-venue: `perps_basis_compare` (перп vs квартальний Deribit vs CME-проксі), `perps_premium_index`.

## Фаза 2 — пам'ять, збирач, алерти, деплой
Мета: сервер «працює, поки сплю».
- [ ] 2.1 `store/`: SQLite (WAL), міграції (`PRAGMA user_version`), доступ через `aiosqlite` або потік-обгортку. Схема (чернетка):
  - `iv_surface(ts, currency, expiry, strike, kind, mark_iv, bid_iv, ask_iv, oi, underlying)` 
  - `atm_term(ts, currency, expiry, atm_iv, rr25, fly25, rr10, fly10)`
  - `funding(ts, venue, symbol, rate_ann, interval_h)`
  - `oi(ts, venue, symbol, oi_usd)`; `basis(ts, currency, expiry, basis_ann)`
  - `liquidations(ts, venue, symbol, side, notional_usd)`
  - `alerts(id, rule_id, ts, payload, delivered)`; `journal(id, ts, symbol, side, thesis, entry, stop, target, tags, status, outcome)`
  - `positions(…)`, `snapshots(ts, kind, json)`
- [ ] 2.2 Збирач (`collector/`): asyncio-задачі з періодами (IV surface 5 хв, funding 1 год, OI 5 хв, basis 5 хв); ретенція/даунсемплінг; метрики самих збірок у `system_health`.
- [ ] 2.3 Ліквідації: WebSocket-читач Binance `!forceOrder@arr`, Bybit, OKX → таблиця `liquidations`; інструмент `liquidations_summary(window)`. **(verify)**
- [ ] 2.4 Історичні інструменти поверх сховища: `iv_rank`, `iv_percentile`, `funding_zscore`, `skew_percentile`.
- [ ] 2.5 Алерти: рушій правил (дивіться 3.1) + канали: Telegram-бот (Bot API; потребує токена й chat_id від власника), email/ntfy як запасні. Антиспам: cooldown, гістерезис, дедуплікація.
- [ ] 2.6 Журнал угод: `journal_add/list/close`, теги, R-мультиплікатор, статистика (win-rate, expectancy).
- [ ] 2.7 Позиції й ризик (ручні/з біржі read-only): net delta/gamma/vega/theta портфеля, сценарії (±10 % спот, ±5 vol-пунктів), margin-estimate.
- [ ] 2.8 Деплой: Dockerfile, `docker-compose`, systemd-unit, `/healthz`, резервне копіювання SQLite (`.backup` + off-site), інструкція для VPS (Hetzner/DO, 1–2 vCPU/2 GB достатньо). 
- [ ] 2.9 HTTP-автентифікація: bearer-токен або mTLS/Tailscale; TLS через Caddy. (Мінімум до публікації порту назовні.)
- [ ] 2.10 Структуровані логи (JSON), ротація, метрики Prometheus (опційно).

## Фаза 3 — правила, стратегії, бектест
- [ ] 3.1 DSL правил: YAML/JSON, умови над метриками (`funding_ann > 0.25 and oi_change_24h > 0.1`), комбінатори all/any, вікна, cooldown. Інструменти `rules_create/list/test/disable`. Безпечний вираз без `eval` (власний парсер/`asteval` у sandbox).
- [ ] 3.2 Бібліотека сигналів: funding-екстремуми, basis-розширення, skew-флип, GEX-flip-наближення, IV-rank, OI+price дивергенції, liquidation-каскади, Polymarket-vs-options розбіжність.
- [ ] 3.3 Стратегії-шаблони (опис + параметри + умови входу/виходу): cash-and-carry, funding-арбітраж між майданчиками, calendar spread, risk reversal, iron condor/short strangle з дельта-хеджем, вола-карри (VRP). Кожна — з калькулятором payoff і ризик-профілем.
- [ ] 3.4 Payoff/сценарії: `strategy_payoff` (багатоногі позиції, греки, P&L-крива, breakevens, ймовірність прибутку з surface).
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
