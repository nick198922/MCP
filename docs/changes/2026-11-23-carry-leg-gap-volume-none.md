# carry_leg_gap: невідомий обсяг не є нульовим, R1 / R4 видно й без 4h-барів (23.11)

Пункт 447 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md); джерело — [рев'ю carry_leg_gap](../reviews/2026-11-23-carry-leg-gap-missing-bar.md), зауваження 2 і 3.

- `scripts/verify_carry_b_leg_gap.py`: `bars()` більше не перетворює невідомий обсяг (`None`) на `0.0`; OHLC як і раніше. У таблиці невідомий обсяг друкується як «—», а не 0. Бари з кешу, записані до зміни, могли вже зберегти `0.0` замість `None`: їх відрізнити не можна.
- `analytics/carry_leg_gap.py`: `row_checks` лишає невідомий обсяг `None` (раніше `float(None)` падав би), а невідомий close вважає відсутнім баром. «R1 zero volume» спрацьовує лише на явний `0`.
- `classify`: у гілці «no data» (немає жодного `gap_indep`) R1 frozen і R4 units тепер рахуються й повертаються у `flags`; вердикт лишається ◯ «no data».
- Тести `test_unknown_volume_is_not_zero_volume` і `test_no_data_still_shows_bar_free_flags`: `uv run pytest -q tests/test_carry_leg_gap.py` — 13 passed; `make lint` чисто. Зі старою поведінкою обидва нові тести падають.
- Повні дані: перезапуск `--date 2026-11-17 --no-fetch --out ~/.cache/tradedesk-agents/carry_b_leg_gap/result-447.json` дав ті самі 3 входи й вердикти, що й `result.json` від 19.11: BIRB ×2 «✗ market, persistent», INIT «✗ market, close noise»; прапорців нема.
