# carry_leg_gap: рядки без 4h-бару більше не дають мовчазного «немає R2» (23.11)

Пункт 383b [постійної картки](../ideas/IDEA-20261102-standing-improvements.md); джерело — рев'ю B пункту 383, зауваження 6.

- `analytics/carry_leg_gap.py`: нова `no_bar_rows` рахує по кожній нозі рядки з ціною рушія, але без придатного 4h-бару біржі (бару нема або close = 0). `classify` повертає `no_bar` (`{"a": n, "b": n}`) і `r2_unknown`. Якщо прапорців дефекту нема, а такі рядки є, вердикт — ◯ «R2 unknown» замість ринкового ✗. Вердикти ✓ / ≈ лишаються, але до класу додається «R2 unknown on N rows».
- `scripts/verify_carry_b_leg_gap.py` друкує для кожного входу «rows without a 4h bar: a N, b N», а в кінці — скільки входів мають R2 unknown.
- Тести `test_missing_bar_makes_r2_unknown` і `test_missing_bar_marks_a_defect_verdict` у `tests/test_carry_leg_gap.py`. Результат `uv run pytest -q tests/test_carry_leg_gap.py`: 11 passed. `make lint` проходить чисто.
- Без пропущених барів код іде тим самим шляхом, тож вердикти ті самі (за рев'ю критика; порівняння з кешем 19.11 не відтворено: той кеш — інший прогін).
