# x_backfill `--stat`: хибні `0`, `""`, `[]` в `oldest` теж потрапляють у `bad_oldest` (22.11)

Пункт 446 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md); джерело — [рев'ю x_backfill bad_oldest](../reviews/2026-11-22-x-backfill-bad-oldest.md), зауваження 2 «не блокує».

- `scripts/x_backfill.py`, `coverage_report`: `if oldest:` → `if oldest is not None:`. Тепер `0`, `""` чи `[]` в `oldest` зробленого місяця вважаються пошкодженням і йдуть у `bad_oldest`, як і обіцяє docstring; `None` (порожній місяць) лишається законним «немає значення».
- Тест `test_coverage_report_lists_falsy_oldest_but_not_none`: `0`, `""`, `[]` → у `bad_oldest`, звіт не падає; `None` → не в `bad_oldest`. Зі старим `if oldest:` тест падає (`assert [] == ['2025-01', '2025-02', '2025-03']`).
- Перевірено: `uv run pytest -q tests/test_x_backfill.py` — 22 passed, `make lint` чисто. Мережі й ключів зміна не торкається.
