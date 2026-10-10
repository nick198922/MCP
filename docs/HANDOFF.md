# HANDOFF — стан проєкту

Оновлено: **2026-10-10** (sess. 2). Версія коду: **0.1.0 + unreleased**. Оновлюйте цей файл наприкінці кожної сесії.

## Мета і контекст
Власник (Микола) будує власний MCP-сервер для торгівлі ф'ючерсами й опціонами, бо доступ до чужого MCP **DOUBLETOP** (хаб із 74 інструментів) скоро закриють. Вимоги, які він озвучив:
- ринки: крипто-перпи/ф'ючерси; крипто-опціони (Deribit); традиційні ф'ючерси й опціони через IBKR; Polymarket;
- мова: Python;
- сервер має **працювати й оновлюватись, поки власник спить** (не залежить від його ноутбука);
- «усі можливості, включно з виконанням ордерів, але поетапно»;
- розробка в цьому (хмарному) середовищі через Claude; власник планує працювати з терміналу, **файли не на його комп'ютері**.

Припущення, які ще не підтверджено власником (не вважати рішеннями): алерти через Telegram; хостинг на VPS; IBKR (на його Маку помічено папки `Jts`/`ntws`, схоже на IB TWS).

## Що зроблено (Фаза 0 + частина Фази 1)
21 інструмент, лише публічні дані, ключі не потрібні:

| Група | Інструменти |
|---|---|
| Система | `system_health(probe)` |
| Ринок (ccxt) | `market_ticker`, `market_tickers` (batch), `market_ohlcv` (+реалізована вола), `market_orderbook` (+slippage), `market_markets` (пошук/фільтр), `market_trades` (aggressor flow + CVD) |
| Перпи | `perps_funding_compare`, `perps_open_interest`, `hyperliquid_overview` |
| Ф'ючерси | `deribit_futures_curve` |
| Опціони | `deribit_vol_surface`, `deribit_option_chain`, `deribit_max_pain`, `deribit_gamma_exposure`, `deribit_expected_move`, `deribit_oi_by_strike`, `prob_from_options` |
| Квант | `odds_devig`, `option_calc`, `touch_probability` |

Ядро: `Http` (ретраї на 429/5xx/timeout, token-bucket, TTL-кеш із single-flight, класифікація помилок), `HealthRegistry`, `Envelope/Evidence`, фейкові транспорти для тестів.

## Як перевірено
- **62 тести, усі зелені** (локально; Python 3.15 через `uv`). Раніше проходили на 3.13/3.14.
- `ruff check` і `ruff format` чисті.
- Black-76 звірено з **реальним тікером Deribit** (delta −0.4535, gamma ≈2e-5, vega 226.95, theta −25.98) і з ціною (mark у BTC = USD-ціна / forward).
- Touch-ймовірності звірено з Monte Carlo (±1.5 %).
- Сервер запускається в обох режимах: stdio-підпроцес і streamable-http (перевірено клієнтом MCP).
- Зразки реальних відповідей Deribit (книга опціонів, ф'ючерси, ticker, instruments) і Hyperliquid (meta, funding history) отримані 2026-10-10 через DOUBLETOP `fetch` і лежать у `tests/fixtures/` та `docs/reference/api-notes.md`.

## Що перевірено живими запитами 2026-10-10
Виявилось, що з сесії Claude Code на Mac власника мережа до бірж **працює** (через `uv run python … httpx/ccxt`; `curl` блокує sandbox). `make smoke` виконано, 9/9 інструментів ok. Деталі в `docs/reference/api-notes.md` (розділ «Живі зразки»):
- **Deribit greeks** збігаються з їхнім ticker до 4 знаків на delta/vega/theta (три BTC-кол біля ATM, 30OCT26).
- **ccxt funding `interval`**: Bybit і OKX повертають `"8h"`; Binance — `null` (наш fallback 8h + warning уже працює).
- **ccxt `openInterestValue`**: лише OKX. Binance/Bybit повертають `null` → `oi_usd=None` + warning. Залишається як покращення (взяти mark звідкись і множити на amount).
- **Hyperliquid `metaAndAssetCtxs`**: усі очікувані поля на місці (`funding/openInterest/markPx/oraclePx/midPx/impactPxs/dayBaseVlm/premium/dayNtlVlm/prevDayPx`), universe 234 монети.
- **OKX order book**: рівні з 4 колонок (включно з `orders_count`) → виправлено в `analytics/orderbook.py` + тест `test_book_tolerates_extra_level_columns`.

## Що ще НЕ перевірено
- CI-файл `.github/workflows/ci.yml` не запускався (гілка з коммітами запушена в `github.com/nick198922/MCP`, CI має бути вже активним — варто глянути `gh run list`).
- `perps_open_interest`: для Binance/Bybit USD-значення досі обчислюється як `openInterestAmount * markPrice`? Ні, зараз просто пропускаємо (`oi_usd=None` + warning). Потенційне покращення.
- Нові інструменти 1.1 (`market_tickers`, `market_markets`, `market_trades`) та 1.12 (`deribit_expected_move`, `deribit_oi_by_strike`) ще не включено в `scripts/live_smoke.py` — додати.

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
