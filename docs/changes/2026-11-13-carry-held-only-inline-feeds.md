# 2026-11-13 · `held_only` на зупиненій книзі: решта тестових фідів (пункт 360)

Пункт 360 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), зауваження 4 [рев'ю пункту 346](../reviews/2026-11-12-carry-feed-held-only.md). Змінено лише тести; код продукту і `execution/` не чіпав.

- **Спільний помічник `_stopped_cycle`** у `tests/test_carry_paper_engine.py`: один цикл зупиненої книги A (`entries_stopped`), перевіряє `status == "done"`, нічого не відкрито і `held_only_seen == [True]`. Його викликають тест п'яти іменованих фідів, два тести зміни інтервалу (на інлайн-класах `Changed`, `Changed4`, `Reported`) і новий `test_vanish_feed_runs_on_a_stopped_book` у `tests/test_carry_v2.py` (`VanishFeed`).
- **Мутаційна перевірка:** без `held_only` у виклику `super().load` в `Changed`, `Reported`, `Changed4` тест падає з `[False] == [True]`; `VanishFeed` без параметра в сигнатурі дає цикл `failed` (`TypeError`). Мутації відкочено.
- `make lint` чисто; `tests/test_carry_paper_engine.py` + `tests/test_carry_v2.py`: 59 passed (було 58).
