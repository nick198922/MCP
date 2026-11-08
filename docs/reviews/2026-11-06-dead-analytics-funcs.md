# Рев'ю: мертві функції analytics/ (пункт 170, BACKLOG 6.33)

Режим C. Вердикт: **approve**.

## Що перевірено самостійно
1. `git diff`: видалено рівно 6 функцій (`beta_of`, `mm_twohop.finite`, `cluster_count`, `cluster_counts`, `summarise_signals`, `nw_mde`) і 3 імпорти (`ols_beta`, `newey_west_se`, `defaultdict`). Інших змін у `src/` немає, тіла решти функцій не чіпались, чисел і реєстрацій у діффі немає.
2. Grep по `*.py` (src, scripts, tests): виклики `finite(`, `.finite`, імпорти, рядкові посилання `"name"` / `'name'` (getattr) — 0 збігів. Усі 6 імен у `docs/` лише в описах (BACKLOG, зміни, старі рев'ю). Жодних посилань поза документами.
3. Імпорти: `ols_beta`, `newey_west_se`, `defaultdict` ніхто не бере через змінені модулі (Grep по `module.name` і по багаторядкових `from ... import (` у tests/scripts; скрипти й тести, що імпортують ці модулі, проходять).
4. `make lint`: чисто (ruff check + format).
5. Тести: `test_alt_downside_beta`, `test_pm_dislocation`, `test_mm_twohop`, `test_smart_money`, `test_sfp_backtest`, `test_pm_longdated`, `test_unused_funcs` — 90 passed. `tests/test_scripts_import.py` (імпорт усіх скриптів, зокрема `verify_alt_downside_beta.py`, `verify_pm_longdated_tail_no.py`) — 227 passed.
6. Документи: картка пункт 170, BACKLOG 6.33 (закреслено, пояснення), `docs/changes/2026-11-06-dead-analytics-funcs.md` — є й відповідають діффу.

## Зауваження
1. (низька, не блокує) У `docs/changes/` та картці написано «233 passed (10 файлів)», я відтворив 90 + 227 по своєму набору; розбіжність лише в наборі файлів, не в результаті.
2. (низька, не блокує) Старі документи (`docs/reviews/2026-11-04-analytics-coverage-audit.md`, `docs/changes/2026-11-04-analytics-coverage-audit.md`) згадують `beta_of` / `nw_mde` як історичний факт; їх правити не треба.

Блокерів немає.
