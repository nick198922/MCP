**Крок:** p — постійна картка вдосконалень (пункт 167, нова тема: межа мережевого I/O, інваріант 2).

**Результат:**
• `tests/test_network_boundary.py` (9 тестів): AST-охоронець імпортів `httpx`, `requests`, `urllib.request`, `aiohttp`, `websockets`, `socket`, `ccxt`.
• Скан: 393 файли `src/tradedesk`, 5 імпортують мережеві бібліотеки, 4 дозволені з причинами, 1 порушення.
• Порушення: `execution/bybit_demo.py` будує власний `ccxt.bybit`; `execution/` не чіпали, записано в `KNOWN_VIOLATIONS` і BACKLOG 6.45.

**Критик:** approve (sonnet), блокерів нема; дрібне — `http.client`/`urllib3` не в списку, голий `import urllib` не ловиться ([рев'ю](../../reviews/2026-11-06-network-boundary.md)).

**Перевірено:** `make lint` чисто, `make test` — 6201 passed, 3 skipped.

**Далі:** `owner`. Питання власнику (BACKLOG 6.45): дозволити `bybit_demo.py` як окрему межу `execution/` чи провести через спільний шар.
