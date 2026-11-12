# 2026-11-12 — `paper_carry_status`: `free_slots` 0, коли нові входи зупинено (пункт 329)

- Джерело: зауваження 4 [рев'ю зупинки carry A](../reviews/2026-11-11-carry-a-stop.md) («не блокує»): з 11.11 рушій A нових позицій не відкриває, а `paper_carry_status` усе ще показував `free_slots` = 5 − відкриті, ніби слоти скоро заповняться.
- Тепер, якщо останній цикл записав `entries_stopped`, `free_slots` = 0; без зупинки (B, або A після ввімкнення `TRADEDESK_PAPER_CARRY_A_ENTRIES_ENABLED`) — як раніше. Опис інструмента про це каже; причина, як і раніше, в `engine.new_entries_stopped`.
- Тижневий звіт `scripts/paper_carry_report.py` не змінено: при зупинці він і так друкує причину замість вільних слотів.
- Тест: `tests/test_carry_paper_engine.py::test_stopped_a_opens_nothing_and_closes_held_positions_as_without_the_stop` — зупинена книга з порожніми слотами дає `free_slots` 0, та сама книга без зупинки — 5 − відкриті (> 0); на старому коді перша перевірка падає.
