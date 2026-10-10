# HANDOFF — стан проєкту

Оновлено: **2026-10-10** (sess. 3). Версія коду: **0.1.0 + unreleased**. Оновлюйте цей файл наприкінці кожної сесії.

## Мета і контекст
Власник (Микола) будує власний MCP-сервер для торгівлі ф'ючерсами й опціонами, бо доступ до чужого MCP **DOUBLETOP** (хаб із 74 інструментів) скоро закриють. Вимоги, які він озвучив:
- ринки: крипто-перпи/ф'ючерси; крипто-опціони (Deribit); традиційні ф'ючерси й опціони через IBKR; Polymarket;
- мова: Python;
- сервер має **працювати й оновлюватись, поки власник спить** (не залежить від його ноутбука);
- «усі можливості, включно з виконанням ордерів, але поетапно»;
- розробка в цьому (хмарному) середовищі через Claude; власник планує працювати з терміналу, **файли не на його комп'ютері**.

Припущення, які ще не підтверджено власником (не вважати рішеннями): алерти через Telegram; хостинг на VPS; IBKR (на його Маку помічено папки `Jts`/`ntws`, схоже на IB TWS).

## Що зроблено (Фаза 0 + частина Фази 1)
35 інструментів, лише публічні дані, ключі не потрібні:

| Група | Інструменти |
|---|---|
| Система | `system_health(probe)` |
| Ринок (ccxt) | `market_ticker`, `market_tickers`, `market_ohlcv`, `market_orderbook`, `market_markets`, `market_trades` |
| Перпи | `perps_funding_compare`, `perps_funding_history`, `perps_long_short_ratio`, `perps_oi_history`, `perps_open_interest`, `perps_basis_compare` |
| Hyperliquid | `hyperliquid_overview`, `hyperliquid_candles`, `hyperliquid_book`, `hyperliquid_trades`, `hyperliquid_funding_history` |
| Ф'ючерси | `deribit_futures_curve` |
| Опціони | `deribit_vol_surface`, `deribit_option_chain`, `deribit_max_pain`, `deribit_gamma_exposure`, `deribit_expected_move`, `deribit_oi_by_strike`, `deribit_dvol`, `deribit_atm_term`, `deribit_skew_term`, `deribit_largest_trades`, `prob_from_options` |
| Квант | `odds_devig`, `option_calc`, `touch_probability` |
| Настрій | `sentiment_fear_greed` |
| Календар | `calendar_expiries` |

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
- Тестів: 80 (+10 за сесію), lint і format чисті.
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
1. Назва/власник GitHub-репозиторію (див. вище).
2. Хостинг для режиму «працює, поки сплю»: VPS (рекомендовано) чи інше.
3. Канал алертів: Telegram-бот?
4. IBKR: так/ні, який тип рахунку, чи готовий тримати IB Gateway на сервері.
5. Результат `make smoke` на його машині.

## Наступні кроки
Див. `docs/BACKLOG.md`. Рекомендований порядок: (1) live-перевірка → правки, (2) решта Фази 1 (історія funding, L/S, Polymarket, DVOL/IV rank), (3) Фаза 2 (SQLite + збирач + алерти + деплой).
