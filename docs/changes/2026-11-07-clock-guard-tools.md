# 2026-11-07 · Охоронець годинника для `tools/` і `providers/` (пункт 219)

Пункт 219 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), інваріант 3 `CLAUDE.md` (час — через підміняємий `rt.now`). Змінено лише тест `tests/test_architecture_imports.py`; `src/`, `core/`, `execution/`, колектори й реєстрації не чіпано.

- **Нове правило `clock-call`.** AST-охоронець тепер забороняє в `src/tradedesk/tools/` і `src/tradedesk/providers/` читання справжнього годинника: `time.time(`, `datetime.now(`, `datetime.utcnow(`. Псевдоніми розпізнаються: `import time as t; t.time()`, `from time import time as wall; wall()`, `from datetime import datetime as D; D.now()`, `dt.datetime.utcnow()`. Перевірку `open(` для `tools/`/`providers/` не вмикали: вона лишається лише для `analytics/`.
- **Порушень 0, виправлено 0, `ALLOWED` не змінено.** У `tools/` час всюди береться з `rt.now()`. У `providers/` є лише `time.perf_counter` (`exchanges.py`, затримка запиту) і `time.monotonic` (`hyperliquid.py`, лімітер): це не годинник, вони дозволені за визначенням правила.
- **Не охоплено:** `date.today()` / `datetime.today()` і `time.time_ns()` правило не ловить (у пункті їх не було; Grep на 07.11 у `tools/` і `providers/` їх не бачить).
- **Тести.** Фікстура з шістьма порушеннями (зокрема `time.time()`, `datetime.now()`, `dt.datetime.utcnow()`) дає `clock-call` у `tools/` і `providers/`, `analytics-call` в `analytics/` і нічого в `core/` та `execution/`. Чистий код із `rt.now()`, `time.monotonic()`, `time.perf_counter()` і `datetime.fromtimestamp(...)` порушень не дає.
- Перевірка: `make lint` пройшов, `uv run pytest -q tests/test_architecture_imports.py`: 7 passed (≈ 3 с). Повний набір не запускали (за завданням).
