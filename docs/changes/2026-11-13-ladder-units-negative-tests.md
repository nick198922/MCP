# 2026-11-13 · Негативні тести unit-файлів ladder (пункт 373)

Пункт 373 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), зауваження 1 [рев'ю пункту 370](../reviews/2026-11-13-ladder-units-test.md). Змінено лише `tests/test_ladder_weekly_units.py`; `src/`, `scripts/`, `deploy/` і `execution/` не чіпав.

- **Мутації тепер у CI:** параметризований `test_ladder_weekly_units_mutation_is_caught` бере справжні `.service` / `.timer`, змінює рівно одну річ і вимагає `AssertionError` від `_assert_units`. Десять випадків: інший шлях `--out-dir`, без `--out-dir`, другий `--out-dir`, перейменований скрипт, закоментований `ExecStart`, другий `ExecStart`, субота замість неділі, `UTC` замість `Europe/Kyiv`, `Persistent=false`, без `Persistent`.
- Кожна мутація спершу перевіряє, що замінюваний фрагмент є у файлі рівно один раз: якщо unit-файл зміниться так, що мутація стане порожньою, тест впаде, а не пройде мовчки.
- Окремий тест: інший `LADDER_SERVER` при незміненому unit-файлі теж ловиться.
- Позитивний тест не змінено. `uv run pytest -q tests/test_ladder_weekly_units.py`: 12 passed; `make lint` чисто.
