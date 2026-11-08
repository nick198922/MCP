# 2026-11-07 · Охоронець годинника: `date.today()` і `time.time_ns()` (пункт 224)

Пункт 224 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), продовження [пункту 219](2026-11-07-clock-guard-tools.md); інваріант 3 `CLAUDE.md` (час — через `rt.now`). Змінено лише `tests/test_architecture_imports.py`; `src/`, `core/`, `execution/`, колектори й реєстрації не чіпано.

- **Правило `clock-call` розширено.** У `src/tradedesk/tools/` і `src/tradedesk/providers/` тепер заборонені й `date.today()`, `datetime.date.today()`, `time.time_ns()` з тими самими псевдонімами імпорту (`dt.date.today()`, `from datetime import date as Day; Day.today()`, `from time import time_ns as ns; ns()`). Оскільки `analytics-call` використовує ту саму функцію, `analytics/` отримує ці заборони теж.
- **Порушень 0, виправлено 0, `ALLOWED` не змінено.** Grep `today\(|time_ns\(` по `src/tradedesk/` знаходить лише імена функцій (`queries_today`, `record_today`, `_today` в `execution/`), а не виклики годинника.
- **Не охоплено:** `datetime.today()` (тобто `datetime.datetime.today()`) правило досі не ловить: у пункті його не було.
- **Тести.** Фікстура з шістьма викликами (`date.today()`, `time.time_ns()`, `datetime.date.today()` і три псевдоніми) дає `clock-call` у `tools/` і `providers/` та нічого в `core/` і `execution/`. Код із `rt.now().date()`, `now.date()`, `date.fromisoformat(...)`, `time.monotonic_ns()` порушень не дає.
- Перевірка: `make lint` пройшов; `uv run pytest -q tests/test_architecture_imports.py`: 9 passed (≈ 3 с). Повний набір не запускали (за завданням).
