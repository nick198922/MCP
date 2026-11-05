# Рев'ю: rss_mb dedupe, пункт 87 (режим C)

Вердикт: **approve**

Перевірено сам: `git diff` для scripts і tests, `make lint` (ruff check і format чисті), `uv run pytest -q tests/test_common_helpers.py` (81 passed).

Знахідки:

1. (ок) У всіх 15 скриптах видалена копія має те саме тіло, що й `common_helpers.rss_mb`: `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024`. Поведінка не змінилась.
2. (ок) `resource.` у 15 змінених скриптах більше не вживається (grep порожній), тож видалений `import resource` не лишив мертвих посилань.
3. (ок) Порядок імпортів і `# noqa: E402` відповідають ruff: `common_helpers` стоїть після `sys.path.insert` там, де він є, а де його немає, це звичайний імпорт, як у вже зміненому першому пакеті.
4. (ок) Зовнішні посилання працюють, бо `rss_mb` лишається іменем у просторі імен модуля:
   - `vrs.rss_mb` у `verify_seller_exhaustion`;
   - `vsp.rss_mb` у `verify_s*_modules`, зокрема у Pool-воркерах;
   - `from verify_session_stop_hunt import rss_mb` у `verify_session_stop_hunt_fx`.
   Нові записи в `RSS_MB_IMPORTED` у `tests/test_common_helpers.py` перевіряють імпорт із `common_helpers` і мають бути червоними, якщо локальну копію повернуть.
5. (низька, не блокує) Скрипти без `sys.path.insert` (`pm_*`, `risk_overlay`, `session_stop_hunt`) покладаються на те, що каталог скрипта є в `sys.path`. Для запуску `python scripts/x.py` це так, і так само працюють уже наявні `premarket_fade` та `tradfi_offhours`. Імпорт як модуля з `tests` без `scripts` у шляху ламався б, але ці скрипти й раніше імпортували сусідні модулі.

Фікси не потрібні.
