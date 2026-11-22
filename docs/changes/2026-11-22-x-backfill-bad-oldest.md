# x_backfill `--stat`: зіпсований `oldest` у `state.json` не валить звіт (22.11)

Пункт 445 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md); джерело — [рев'ю 21.11](../reviews/2026-11-21-x-backfill-suspect-months.md), зауваження 1–2 «не блокує».

- `scripts/x_backfill.py`, `coverage_report`: якщо `oldest` зробленого місяця не розбирається як `%Y-%m-%dT%H:%M:%SZ` (ручна правка, число замість рядка), місяць потрапляє в нове поле `bad_oldest` і не оцінюється як підозрілий; раніше `ValueError` / `TypeError` обривав увесь `--stat`.
- Довгий рядок з обчисленням `gap` розбито; docstring і `--help` згадують `bad_oldest`.
- Тест `test_coverage_report_lists_unparsable_oldest_instead_of_crashing` (рядок з іншим форматом і число поряд із правильним підозрілим місяцем); порожній звіт в існуючому тесті доповнено полем.
- Перевірено: `uv run pytest -q tests/test_x_backfill.py` — 21 passed, `make lint` чисто; на реальному архіві Trader_XO `bad_oldest=[]`, `suspect_months=[]`, 107 місяців — як і до зміни. Мережі й ключів зміна не торкається.
