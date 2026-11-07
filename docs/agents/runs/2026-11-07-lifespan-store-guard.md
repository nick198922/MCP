**Крок:** p — постійна картка `IDEA-20261102-standing-improvements` (лишався лише заблокований 8a, додано пункт 222 за «Як поповнювати»).

**Результат:**
• `server.py`: `store.init()` у lifespan в `try/except STORE_ERRORS`; зіпсована БД більше не обриває з'єднання, планувальник колекторів не стартує, інструменти без сховища працюють, зі сховищем дають `ok=false`/`missing: store`.
• Тести: 2 нових у `tests/test_provider_lifecycle.py` (зіпсована БД, справна БД). `make lint` ✓, `make test` 6984 passed.
• Не ловляться винятки поза `STORE_ERRORS`; `scheduler.start()` лишився поза `try`.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-07-lifespan-store-guard.md); регрес ловиться тестом.

**Далі:** `owner` — гілка на рев'ю. Нових питань немає.
