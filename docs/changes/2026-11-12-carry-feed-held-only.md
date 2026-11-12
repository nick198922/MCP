# 2026-11-12 · Фіди carry приймають `held_only` (пункт 346)

Пункт 346 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), зауваження 4 [рев'ю пункту 342](../reviews/2026-11-12-carry-a-idle-cycles.md). Змінено docstring `CarryFeed` у `src/tradedesk/collector/carry_paper_engine.py` і тести. `execution/` і `core/` не чіпав; поведінка рушія та фіду не змінюється.

- **Контракт.** Docstring протоколу `CarryFeed` тепер прямо каже: кожен фід і кожен підклас, що перевизначає `load`, має приймати ключовий аргумент `held_only` (рушій передає його на зупиненій книзі A), інакше цикл падає з `TypeError`.
- **Тестові фіди.** Перевизначення `load` у `tests/test_carry_paper_engine.py` (`TruncatedFeed`, `Changed`, `Changed4`, `Reported`, `DroppingFeed`, `PermanentHoleFeed`, `IncompleteHoleFeed`, `MidWindowHoleFeed`) і `VanishFeed` у `tests/test_carry_v2.py` приймають `held_only` і передають його далі в `FixtureFeed.load`; той записує отримані значення в `held_only_seen`.
- **Новий тест** `test_stopped_a_runs_with_fixture_feed_subclasses`: книга A з відкритими позиціями, далі по одному зупиненому циклу з п'ятьма підкласами; кожен цикл `done`, нічого не відкрито, `held_only=True` дійшов до `FixtureFeed.load`.
- `make lint` чисто; `tests/test_carry_paper_engine.py` + `tests/test_carry_paper_live.py`: 57 passed.
