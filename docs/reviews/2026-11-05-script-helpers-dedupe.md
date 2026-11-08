# Рев'ю: пункт 123, дедуплікація `rss()` у 12 скриптах (2026-11-05)

**Вердикт: approve.** Блокерів немає.

Перевірено сам:
- `git diff` по `scripts/` і `tests/`: у кожному з 12 файлів змінилися лише тіло `rss()` та прибраний `import resource`. Префікси збігаються зі старими: `peak RSS ` (9 файлів), `rss ` (pump_fade, sector_pair_spread), без префікса (carry_modules). Формат `:.0f MB` лишився. Для `carry_modules` рядок `N MB` теж той самий.
- `cmn.rss_mb()` у `scripts/common_helpers.py:59-61` дорівнює `getrusage(RUSAGE_SELF).ru_maxrss / 1024`, тобто число те саме, що й було. Поведінка не змінилась.
- Імпорт: у всіх 12 файлах рівно один `import common_helpers as cmn`. Він стоїть після `sys.path.insert(0, scripts_dir)`, як в інших скриптах, з `# noqa: E402`. У трьох файлах (market_flow_reader, pump_fade, carry_modules) його додано, в решті він уже був.
- Залишків `resource.` у цих файлах немає (прибраний імпорт не ламає жодного використання, `make lint` це підтверджує).
- `make lint`: ruff check і format чисті.
- `uv run pytest -q tests/test_common_helpers.py tests/test_scripts_import.py`: 313 passed.
- Тест змістовний. Він підміняє `cmn.resource.getrusage` на шести значеннях, включно з краями округлення (511/512, 1537), і порівнює `mod.rss()` з виразом прибраного тіла та з префіксом для кожного модуля. Якщо префікс чи формат зламати, тест впаде. Перевірка `mod.cmn is cmn` та `not hasattr(mod, "resource")` фіксує, що старий шлях не повернувся.
- Журнал змін `docs/changes/2026-11-05-script-helpers-dedupe.md` відповідає диффу: перелік скриптів і префіксів, що додано `cmn`, що пропущено і чому. Зміни лише в `scripts/` і `tests/`, `src/` не чіпали.

Зауваження (не блокують):
1. Тест використовує `monkeypatch` на `cmn.resource.getrusage`, тобто на глобальний модуль `resource`. Monkeypatch його відновлює, тож це безпечно, але патч зачіпає й інші користувачі `resource` на час тесту. Окремо виправляти не потрібно.
2. Зміни в `docs/BACKLOG.md`, `docs/HANDOFF.md` і картці standing-improvements я бегло перевірив лише по `--stat` (по 1-2 рядки). Вони відповідають пункту 123.
