# 2026-11-07 · Охоронець годинника: `datetime.today()` (пункт 231)

Пункт 231 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), закриває лишок [пункту 224](2026-11-07-clock-guard-today.md); інваріант 3 `CLAUDE.md` (час — через `rt.now`). Змінено лише `tests/test_architecture_imports.py`; `src/`, `core/`, `execution/`, колектори й реєстрації не чіпано.

- **Правило `clock-call` розширено.** У `src/tradedesk/tools/` і `src/tradedesk/providers/` тепер заборонені й `datetime.today()` та `datetime.datetime.today()` з тими самими псевдонімами імпорту (`dt.datetime.today()`, `from datetime import datetime as D; D.today()`). `datetime.today()` дає наївний локальний час сервера в обхід `rt.now`. Через спільну функцію `analytics/` (правило `analytics-call`) отримує цю заборону теж.
- **Порушень 0, виправлено 0, `ALLOWED` не змінено.** Grep `today\(` по `src/tradedesk/` знаходить лише імена функцій (`queries_today`, `record_today`, `_today` в `execution/`).
- **Тести (+2).** Дві фікстури (`import datetime` і `from datetime import datetime` зв'язують одне ім'я, тому окремо) з чотирма викликами дають `clock-call` у `tools/` і `providers/` та нічого в `core/` і `execution/`. Код із `rt.now()`, `now.replace(...)`, `datetime.fromisoformat(...)`, `dt.datetime.combine(...)` порушень не дає.
- Перевірка: `make lint` пройшов; `uv run pytest -q tests/test_architecture_imports.py`: 11 passed (≈ 3 с). Повний набір не запускали (за завданням).
