**Крок:** p — постійна картка `IDEA-20261102-standing-improvements` (Порядок 1); бюджет тижня 91 %, тож лише крок p. Інші пріоритетні картки заблоковані (час, дані сервера, рішення власника); відкритих незаблокованих пунктів у картці не лишилось — додав пункт 34 з BACKLOG 6.4.

**Результат:**
• Пункт 34: `mode=ro` без екранування шляху в `dashboard.table_freshness`, `bybit_import.py`, `verify_free_supply_history.py` замінено на `connect_ro`; копії `connect_ro` / `has_table` у `verify_demo_execution.py` / `cycle_regime_eval.py` — на спільні.
• Тести `tests/test_common_helpers_callers.py` (шлях з пробілом, `?`, `#`).
• Знайдено помилку, не виправлено: `ZeroDivisionError` у `bybit_import.stats` без збиткових угод — у BACKLOG 6.4.
• `make lint` чистий, `make test`: 4232 passed, 2 skipped (мій прогін).

**Критик:** C (sonnet) — approve, блокерів немає, [рев'ю](../../reviews/2026-11-03-scripts-dedup-3.md).

**Далі:** `owner` — можна зливати. Питань нових немає (відкрите Q-20261102-1 чекає відповіді).
