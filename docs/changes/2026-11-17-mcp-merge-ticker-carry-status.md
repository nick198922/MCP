# 2026-11-17 — `market_ticker` → `market_tickers`, `carry_paper_status` → `paper_carry_status(strategy="manual")` (картка mcp-cleanup, пункт 2, друге й третє злиття)

Рішення власника Q-20261114-1 (А1), картка [`IDEA-20261114-mcp-cleanup`](../ideas/IDEA-20261114-mcp-cleanup.md). Два інструменти прибрано з MCP, їхню роботу роблять сусіди з ширшими даними. Інструментів було 104, стало 102.

## Стара → нова

| Було | Стало |
|---|---|
| `market_ticker(exchange, symbol)` | `market_tickers(exchange, symbols="<symbol>")` або `symbols=["<symbol>"]` |
| `market_tickers(exchange, symbols?, limit?)` | без змін (кілька символів або жодного — один виклик `fetch_tickers`, як і раніше) |
| `carry_paper_status(id?, include_closed?)` | `paper_carry_status(strategy="manual", id?, include_closed?)` |
| `paper_carry_status(strategy="A" / "B" / "both", include_closed?)` | без змін |

## `market_tickers`

- `symbols` тепер приймає рядок або список. Один символ (рядок, список з одного, повтори згортаються) іде через `fetch_ticker`, як старий `market_ticker`, тож працює й на біржах без batch-тікерів (у тестах — kraken, наживо — BingX-форекс). Кілька символів або жодного — `fetch_tickers`, код і відповідь ті самі, що були.
- Залишили `market_tickers`, бо він ширший: сортування за обсягом, `limit`, `top_movers`, і його вже викликає знімок асистента (`collector/assistant_snapshot.py`, три символи — шлях batch, не змінився).
- **Форма відповіді для одного символу змінилась:** було `data = {symbol, last, bid, ask, …, spread_bps}`, стало `data = {count: 1, returned: 1, tickers: [той самий рядок], top_movers: [...]}`; рядок має ті самі поля й ті самі числа (`spread_bps` за тією ж формулою). Читати `data.tickers[0]`. Збій біржі — як і раніше: `ok=false`, у `missing` поле `ticker` (один символ) або `tickers` (batch); відповідь не масив, а список — `data_unavailable` «unexpected response type list» для обох шляхів (раніше batch без цієї перевірки падав би на `.items()`).
- Опис інструмента тепер з одиницями (ціни у валюті котирування, `percentage` у відсотках, `timestamp` у мс, `spread_bps` у базисних пунктах) — закриває й зауваження аудиту про опис `market_ticker` у 92 символи.

## `paper_carry_status(strategy="manual")`

- Це ручні пари перп-перп, відкриті `carry_paper_open` і помічені живими даними (funding, 4h / 15m-бари, last). Відповідь, попередження (`PAPER_WARNING` з результатом бектесту −8.7 % APR) і `missing` — ті самі, що в `carry_paper_status`: тіло старого інструмента без змін перенесено у функцію `manual_book_status` у `tools/carry_paper.py`, `paper_carry_status` її викликає.
- Книги рушія A / B / `both` відповідають, як і раніше (за замовчуванням `both`, ручні пари туди не додаються: це окрема книга з живим маркуванням, у неї інша вартість виклику й інші попередження).
- `id` — номер ручної пари; з `strategy` A / B / both він дає `ok=false`, `missing: id / invalid_input` («id selects a manual pair: use it with strategy='manual'»), а не мовчки ігнорується. `include_closed`: для книг рушія — 50 останніх закритих на книгу, для `manual` — до 200 закритих / скасованих пар (як було).
- Залишили назву `paper_carry_status`: у ревізії інструментів 14.11 вона позначена «лишити … після злиття — єдиний статус carry».
- `carry_paper_open` і `carry_paper_close` лишились (їх прибирає пункт 3 картки); їхні описи тепер посилаються на `paper_carry_status(strategy='manual')`.

## Перевірка

- Тести: `test_server` (новий `test_market_tickers_one_symbol_uses_fetch_ticker`: рядок, список з одного й повтор дають той самий рядок, kraken без batch відповідає; збій біржі — `auth` у `missing`), `test_carry_paper_live` (`id` з книгою A → `invalid_input`; `manual` на порожній базі — `ok=true`, власне попередження), `test_tools_no_store` (новий: `manual` при збої сховища — `missing: store / backend_error`, без винятку); виклики старих назв у `test_carry_paper`, `test_tool_failure_extra_8/22/23/24`, `test_json_cells_guard`, `test_tools_malformed_200`, `test_provider_lifecycle`, `test_tools_never_raise` переведено на нові.
- Наживо 17.11 з сервера через `build_server()`: `market_tickers` binanceusdm `"BTC/USDT:USDT"` — `ok=true`, 1 рядок, last 63 820.1 (`spread_bps` немає: тікер Binance USDⓈ-M не дає bid / ask — так само було в `market_ticker`); bybit `["ETH/USDT:USDT"]` — last 1793.28, спред 0.056 bp; bingx `["NCFXEUR2USD/USDT:USDT"]` — last 1.10121, спред 0.091 bp; три символи binanceusdm — `ok=true`, 3 рядки. `paper_carry_status(strategy="manual")` — `ok=true`, 0 відкритих, 0 закритих; `strategy="A", id=1` — `ok=false`, `invalid_input`; без параметрів — книги A і B, `missing` порожній.

## Хто викликав старі назви

- Переведено: `scripts/live_smoke.py` (два виклики `market_ticker`), тести вище, описи `carry_paper_open` / `carry_paper_close`, `README.md`, `docs/PARITY.md`.
- `scripts/mcp_call.py` (`WRITE_TOOLS`) і `scripts/nightly_agents.sh` жодної з двох старих назв не містять — правок не треба; `.claude/*` теж.
- Старі назви лишились в історичних записах (`CHANGELOG.md`, `docs/changes/`, `docs/reviews/`, `docs/agents/runs/`, `docs/reference/doubletop-catalog.md`) — їх не переписуємо. Таблиця сховища `carry_paper` і її індекс `carry_paper_status_opened` (`store/schema.py`) — це назва колонки й індексу, не інструмента.
- Служба MCP підхопить зміну після злиття гілки й перезапуску; чат і агенти до того бачать старі назви.
