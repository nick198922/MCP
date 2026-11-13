# Рев'ю: held_only в інлайн-фідах carry (пункт 360)

Вердикт: **approve**

Режим C. Джерело: зауваження 4 з `docs/reviews/2026-11-12-carry-feed-held-only.md`.

Перевірено самостійно:
1. `git diff`: змінено тільки `tests/test_carry_paper_engine.py`, `tests/test_carry_v2.py`, рядок 360 картки і запис у `docs/changes/`. `src/` і `execution/` не торкнуто.
2. `make lint`: чисто. `uv run pytest -q tests/test_carry_paper_engine.py tests/test_carry_v2.py`: 59 passed.
3. Тести змістовні. Спільний `_stopped_cycle` перевіряє `status == "done"`, `opened == []` і `held_only_seen == [True]`. Якщо перевизначення `load` в `Changed`, `Changed4`, `Reported` чи `VanishFeed` не прийме `held_only` (TypeError) або не передасть його в `super().load`, `held_only_seen` стане `[False]` і тест впаде. Усі чотири перевизначення (рядки 448, 456, 483 у engine-тесті і 142 у v2) передають `held_only=held_only`. Мутації я сам не проганяв, спиралися на твердження автора і на читання коду.
4. Жодну наявну перевірку не послаблено. Тіло циклу в `test_stopped_a_runs_with_fixture_feed_subclasses` просто винесено в помічник з тими самими трьома asserts.
5. Циклічного імпорту немає: `test_carry_v2` уже імпортував `test_carry_paper_engine`, додано лише `_stopped_cycle`. Зворотного імпорту немає.

Зауваження (не блокує):
- Низька. У `test_vanish_feed_runs_on_a_stopped_book` `VanishFeed` створюється з `cut=times[i] - DECISION_DELAY_MS` і без позиції, що зникає у відповідному циклі. Для перевірки прапорця цього досить, поведінку vanish цей тест не перевіряє.
