**Крок:** p, картка `book-walls` (Порядок 1), пункт 1: розробник → критик C. Картки з Порядком 0 заблоковані: `options-playbook` п. 4 без нових правил; `agents-mcp-access` чекає Q-20261114-2; `x-traders` — гілка вже чекає власника; `profit-search` п. 2 і `structural-edges-depth` 8f чекають даних; `mcp-cleanup` п. 3 — лишились дії власника.

**Результат:**
• Перший зріз колектора стін: Binance USD-M і Bybit перп, BTC і ETH; вимкнено за замовчуванням (`TRADEDESK_COLLECT_BOOK_WALLS_ENABLED`).
• Схема v41 (події стін, хвилинна глибина, з'єднання), чисті `analytics/book_walls.py`, провайдер `orderbook_stream.py`, 37 тестів.
• Відхилення від картки (перевірено живо на сервері): Bybit `orderbook.1000` замість `.200`; ±2 % недосяжні на публічних стрімах (видимість ±0.2–1 %); пороги 25 BTC / 750 ETH; ≈ 25 МБ/добу.
• Не зроблено: Binance спот і Coinbase level2; калібровка порогів і CPU на першій добі. Пункт 1 лишається `[ ]`.

**Критик:** перше рев'ю — 4 блокери (зависання `merge_streams`, ліміт REST-знімків Binance, відкидання коротких `pulled_near`, мітка журналу); виправлено, повторне рев'ю — approve. Лишились три дрібні зауваження (R1–R3) у рев'ю.

**Далі:** `owner` — зріз схвалено. Власнику: після злиття додати `TRADEDESK_COLLECT_BOOK_WALLS_ENABLED=1` у `deploy/features.env`; наступним прогоном — Binance спот і Coinbase. `make lint && make test`: 10366 passed.
