**Крок:** e/постійна картка — пункт 127 `IDEA-20261102-standing-improvements` (усе інше заблоковано або на паузі).

**Результат:**
• Прямі тести для 10 функцій `analytics/` без покриття: `tests/test_analytics_direct_5.py`, 26 тестів, еталони пораховано вручну.
• Помилок не знайдено, `src/` не змінено; лічильник BACKLOG 6.9: 88 з 2163.
• `make lint` чисто, `make test`: 5140 passed, 3 skipped.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-06-analytics-direct-tests-5.md); дрібні зауваження не блокують.

**Далі:** `owner`. Питань до власника немає; на гілці `agent/2026-11-04-library-seller-exhaustion-card` досі чекає PR.
