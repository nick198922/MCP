**Крок:** p, постійна картка `standing-improvements` (Порядок 1): відкритим лишався тільки 8a (чекає сервера), тож додано пункт 112.

**Результат:**
• Новий `tests/test_architecture_imports.py` (AST): `analytics/` без I/O-імпортів і без `open` / `time.time` / `datetime.now`; `httpx` лише в `core/http.py` і `runtime.py`; `ccxt` лише в `providers/exchanges.py`; `tools/` не імпортує `execution`.
• Знайдено 2 порушення, обидва в `execution/bybit_demo.py` (власний клієнт ccxt для демо-рахунку). Внесені в allowlist з причиною, BACKLOG 6.20; `src/` не змінено.
• `make lint` чистий, `make test`: 4770 passed, 3 skipped.

**Критик:** C (sonnet), approve. Зауваження без блокування: псевдоніми (`from time import time as t`) не ловляться; `websockets` не входить у правило мережі; `os` / `pathlib` не заборонені в `analytics/`.

**Далі:** `owner`. Питань немає. Рішення власника: чи переносити клієнт Bybit demo на спільний шар провайдерів (BACKLOG 6.20).
