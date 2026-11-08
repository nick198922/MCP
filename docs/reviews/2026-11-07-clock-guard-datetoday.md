# Рев'ю пункту 231: `clock-call` ловить `datetime.today()`

Режим C. Вердикт: **approve**.

## Перевірено самостійно
- `git diff`: змінено лише `tests/test_architecture_imports.py`, `docs/changes/2026-11-07-clock-guard-datetoday.md` і рядок картки. `src/`, `core/`, `execution/` не чіпали.
- `make lint`: чисто.
- `uv run pytest -q tests/test_architecture_imports.py`: 11 passed.
- Твердження «0 порушень у `src/`»: Grep `\.today\(|\.utcnow\(` по `src/` дає 0 збігів. `ALLOWED` не змінювався, і тест не падає на зайвих записах, тож твердження правдиве.

## Знахідки
1. (інфо) Правило: у `CLOCK_TAILS` додано `["datetime", "today"]`. Фікстури покривають `import datetime` (`datetime.datetime.today`), `import datetime as dt` (`dt.datetime.today`), `from datetime import datetime` і `... as D` (`D.today`). Усі дають `clock-call` у `tools/` і `providers/`, а в `core/` і `execution/` порушень немає. Це відповідає задачі.
2. (інфо) Хибні спрацювання: тест із чистим кодом (`rt.now()`, `datetime.fromisoformat`, `dt.datetime.combine`) порушень не дає. `date.today` лишається в `CLOCK_TAILS` і працює як раніше (тести пункту 224 проходять). Виклик `.today` на інших об'єктах, наприклад `pandas.Timestamp.today`, не збігається з хвостом `datetime.today`.
3. (низька) Тест `test_clock_guard_flags_datetime_today` порівнює точну множину `(rule, call)`, тож пропуск або зайве спрацювання його провалять. Побічних правил у фікстурах немає.

Блокерів немає.
