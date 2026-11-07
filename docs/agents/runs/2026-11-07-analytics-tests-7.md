**Крок:** p — постійна картка `IDEA-20261102-standing-improvements` (відкритих пунктів лишалось менше трьох); додано й виконано пункт 199.

**Результат:**
• `tests/test_analytics_direct_10.py`: 12 тестів на 11 публічних функцій `analytics/` (s12/s16 модулі, `session_stop_hunt`, `fresh_confounders`, `trade_plan.usable_expires`, індикатори сита).
• Еталони пораховано вручну або аналітично; помилок у `src/` не знайдено, `src/` не змінено.
• Лічильник BACKLOG 6.9: 44 → 33 із 2173 (реально 35 через дублікат імені `close_at`).
• Зауваження: `usable_expires(np.int64)` дає `None`; на журнал не впливає (SQLite віддає `int`).

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-07-analytics-tests-7.md). Моя перевірка: `make lint` чисто, `make test` — 6852 passed, 3 skipped.

**Далі:** owner — крок завершено, питань немає.
