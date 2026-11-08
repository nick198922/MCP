# 05.11 — Охоронець меж імпортів, інваріанти 2–3 (пункт 112, BACKLOG 6.20)

- Новий `tests/test_architecture_imports.py`: розбирає AST усіх модулів `src/tradedesk/` без мережі й без імпорту коду. Враховано й імпорти всередині функцій, і відносні імпорти.
- Правила такі. `analytics/` не імпортує `httpx`, `requests`, `urllib.request`, `socket`, `sqlite3`, `ccxt`, `websockets`, `aiohttp`, `subprocess`, а також `tradedesk.providers` / `tools` / `store` / `execution`. Він не викликає `open(`, `time.time(`, `datetime.now(`, `datetime.utcnow(`. `httpx` імпортують лише `core/http.py` і `runtime.py`, `ccxt` — лише `providers/exchanges.py`. `tools/` не імпортують `tradedesk.execution`.
- Знайдено 2 порушення, обидва в `execution/bybit_demo.py` (`ccxt`, `ccxt.async_support`): це власний клієнт демо-рахунку. Вони в `ALLOWED` з причиною й у BACKLOG 6.20. `analytics/` і `tools/` чисті.
- Тест падає і на новому порушенні, і на застарілому записі в `ALLOWED`. Самоперевірка на синтетичному коді підтверджує, що перевірка ловить кожне правило і не спрацьовує на `open(` у docstring.
- Код у `src/` не змінено.
