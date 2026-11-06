**Крок:** постійна картка standing-improvements, новий пункт 172 (BACKLOG 6.9): тести для публічних функцій `analytics/` без покриття.

**Результат:**
• `tests/test_analytics_direct_6.py`: 17 тестів для 10 функцій (`carry_observers`, `spread_observers`, `target_weights`, `basket_components`, `last_friday`, `symbol_seed`, `funding_r`, `signed_notional`, `tau_of_hours`, `fmt_bytes`); еталони пораховані вручну.
• Помилок у `src/` не знайдено, `src/` не змінювався.
• Лічильник BACKLOG 6.9: лишилось 74 із 2171 (критик перерахував AST-методом; розробник спершу вказав 67).
• `make lint` чистий, `make test`: 6233 passed, 3 skipped.

**Критик:** (sonnet, режим C) request changes лише через лічильник; виправлено координатором, код і тести прийнятні ([рев'ю](../../reviews/2026-11-06-analytics-tests-3.md)).

**Далі:** `owner`. Відкритий лише 8a (чекає даних сервера); при наступному прогоні додати пункти до картки.
