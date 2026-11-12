# 2026-11-12 — звіт «дірки чи тиша»: тест ловить клас від типового E (пункт 349)

Зауваження 4 рев'ю `docs/reviews/2026-11-12-liquidation-stream-hour-log.md`.

- `test_cycle_flags_change_the_expected_time` (`tests/test_liquidation_gaps_report.py`): година з'єднання тепер 3100 с замість 3218 с. За прапорців `--window-seconds 60 --restart-seconds 5` це 93 % від очікуваних 3323 с, клас `quiet market`; за типового циклу 600 + 5 с це 87 % від 3570 с, клас `unclear` (тест перевіряє обидва).
- Перевірено мутантом: якщо в `connection_notes` викликати `classify(c, f)` без `expected_s`, тест падає; на поточному коді проходить. Мутацію відкочено.
- Код продукту (`scripts/liquidation_gaps_report.py`) не змінювався, `execution/` не чіпали.
