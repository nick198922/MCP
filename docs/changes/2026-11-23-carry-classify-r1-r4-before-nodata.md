# carry_leg_gap: R1 / R4 до виходу «no data» — уже зроблено (23.11)

Пункт 451 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md); джерело — [рев'ю carry_leg_gap missing-bar](../reviews/2026-11-23-carry-leg-gap-missing-bar.md), зауваження 2.

- Код не мінявся: зауваження 2 уже закрив пункт 447 (коміт `4c872aad`, [зміни](2026-11-23-carry-leg-gap-volume-none.md)), а не 383b. Пункт 383b додав лише `no_bar` / `r2_unknown`.
- `classify` живе в `src/tradedesk/analytics/carry_leg_gap.py`, а не в `scripts/verify_carry_b_leg_gap.py` (скрипт викликає `g.classify`). У гілці «no data» (рядки 137–139) R1 frozen і R4 units рахуються через `_frozen` / `_units` і повертаються у `flags`; вердикт лишається ◯ «no data».
- Тест: `test_no_data_still_shows_bar_free_flags` у `tests/test_carry_leg_gap.py`. Нога без жодного 4h-бару дає `("◯", "no data", ["R1 frozen a"])` і `("◯", "no data", ["R4 units"])`, порожні рядки дають `flags == []`.
- Перевірка 23.11: `uv run pytest -q tests/test_carry_leg_gap.py` — 16 passed; `make lint` чисто.
