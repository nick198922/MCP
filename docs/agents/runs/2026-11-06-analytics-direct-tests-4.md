**Крок:** p, постійна картка вдосконалень, новий пункт 126 (прямі тести функцій `analytics/`).

**Результат:**
• 25 прямих тестів для 10 публічних функцій `analytics/` без жодної згадки в `tests/` (`flow_model`, `funding_settlement`, `funding_crowding`, `xs_factors`, `strangle_manage`, `instruments`).
• Еталони пораховано вручну; `src/` не змінено, помилок не знайдено.
• Лічильник BACKLOG 6.9 перераховано: 98 із 2163.
• `make lint` чистий, `make test`: 5114 passed, 3 skipped.

**Критик:** C (sonnet), approve. Блокерів немає; виправлено лише хибний лічильник у BACKLOG ([рев'ю](../../reviews/2026-11-06-analytics-direct-tests-4.md)).

**Далі:** owner. Пункт 8a чекає журналів сервера; нових питань немає.
