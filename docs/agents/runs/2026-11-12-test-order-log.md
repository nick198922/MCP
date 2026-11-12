**Крок:** p/постійна картка (`standing-improvements`, пункт 333): картка profit-search (Порядок 0) вичерпана — усі картки партій 1–5 оброблені, п. 2 і 5 без нових даних/✓.

**Результат:**
• Лог перетасованих прогонів тепер показує `test order: shuffled, --order-seed N` і з `-q`, і під xdist; без xdist ще й перші 3 тести порядку.
• Зміни: `scripts/test_order_check.py`, `tests/test_test_order_check.py`, [зміни](../../changes/2026-11-12-test-order-log.md).
• `make lint` ✓, `make test` 9623 passed, 3 skipped.

**Критик:** approve (C, sonnet), 3 зауваження «не блокує»; [рев'ю](../../reviews/2026-11-12-test-order-log.md).

**Далі:** owner. Відкрите питання без відповіді: Q-20261111-1 (контакт для User-Agent Wikimedia).
