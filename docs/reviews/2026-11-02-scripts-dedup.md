# Рев'ю (режим C): дублікати хелперів у scripts/ (пункт 18 standing-improvements)

**Вердикт: approve.** Блокерів немає.

## Що перевірив сам
- `git status` / `git diff`: змінено 8 скриптів, 3 документи (BACKLOG, HANDOFF, картка), нові `scripts/common_helpers.py`, `tests/test_common_helpers.py`, `docs/changes/2026-11-02-scripts-dedup.md`. `execution/`, колектори, `scripts/server/`, `agents_*`, `nightly_agents.sh`, `registry.csv` не чіпали.
- `make lint`: ruff check і format чисті.
- `uv run pytest -q tests/test_common_helpers.py tests/test_dashboard_*.py tests/test_checkpoint_report.py tests/test_collector_health.py tests/test_demo_vs_backtest.py`: 164 passed.
- `python3 -I scripts/dashboard.py --help` працює: `common_helpers` імпортується через `sys.path.insert(0, HERE)`, який у панелі вже був, і використовує лише stdlib (`sqlite3`, `urllib.parse`, `pathlib`).

## Поведінка не змінилась (звірено по diff)
1. `connect_ro`: той самий URI `file:{quote(str(path))}?mode=ro`, `uri=True`, `timeout=5`. `mode=ro` збережено в усіх 11 замінених місцях; записів немає. Обгортки з `exists()` у `checkpoint_report` і `collector_health` лишились, тож `FileNotFoundError` той самий.
2. `open_ro`: ті самі гілки (`missing`, `unreadable: <ім'я винятку>`), `exists()` усередині `try`, перехоплюються `sqlite3.Error` і `OSError`, як у 7 оригіналах. У `demo_vs_backtest.py` (`load_closed`, `load_not_executed`) пролог лишено, замінено тільки рядок з'єднання; тексти причин ті самі.
3. `has_table`: той самий запит до `sqlite_master`, але з параметром замість вбудованої літералі, що еквівалентно. У `probe` та інших завантажувачах `no_table` повертається там само.
4. Лічильники в changes (11 / 7 / 4 копії; ще 2 у `verify_demo_execution.py`) збігаються з diff і `grep`.

## Тести
Осмислені: читання, відмова в записі (`readonly`), шлях із пробілом, `?`, `#`, `%` (без екранування файл читався б інакше або створювався), перевірка, що поруч нічого не створено, не створює відсутній файл, URI і `timeout` через spy, обидві причини `open_ro` включно з `PermissionError`, `has_table` не плутає view з таблицею і не піддається ін'єкції. Вони впали б, якби хелпер зламали.

## Зауваження (не блокують)
1. **Низька.** `collector_health.py` і `demo_vs_backtest.py` роблять `import common_helpers` без власного `sys.path.insert`; це працює, бо запускаються як скрипти (каталог скрипта в `sys.path`), а тести проходять. Імпорт цих модулів з іншого місця без `scripts/` у `sys.path` дасть `ImportError`. Правка не потрібна; варто пам'ятати.
2. **Низька.** `checkpoint_report.py`: імпорт `common_helpers` стоїть після `sys.path.insert`, з `noqa` — коректно.
3. Картка: відмітка пункту 18 чесна («чекає рев'ю критика C», не `approve`); після цього рев'ю запускач може дописати `критик C: approve` і посилання на цей файл. «Не більше трьох хелперів» дотримано (три, в одному модулі `scripts/`). Користь реальна, але скромна (менше копій одного небезпечного рядка), вона названа в changes.
4. Change-файл точний; перелік решти дублікатів відповідає дійсності (перевірено вибірково: `verify_demo_execution.py:32,50`).
