**Крок:** постійна картка standing-improvements, новий пункт 70 (BACKLOG 6.9): тести для чистих функцій `analytics/`, що йдуть у числа форвардів.

**Результат:**
• 23 тести в `tests/test_analytics_forward_numbers.py` для 11 функцій (`carry_rules`, `carry_delisting.settle_price`, `carry_modules`, `pm_gap_paper`, `polymarket.devig_multi`), еталони пораховано вручну.
• Знайдено помилку (не виправлено, BACKLOG 6.11): `normalise_market` зсуває devig-ціни, якщо одна ціна не число.
• Функцій без тесту стало 113 (було 126). `src/` не змінено.
• `make lint && make test`: 4497 passed, 2 skipped.

**Критик:** C (sonnet) — approve, блокерів немає ([рев'ю](../../reviews/2026-11-05-forward-analytics-tests.md)).

**Далі:** `owner`. Виправлення `normalise_market` (6.11) — наступний пункт для розробника.
