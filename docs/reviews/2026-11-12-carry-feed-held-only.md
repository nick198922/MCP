# Рев'ю: фіди carry і `held_only` (пункт 346)

Режим C, незакомічені зміни. Вердикт: **approve**. Блокерів нема.

Перевірено самостійно:
- `make lint`: чисто.
- `uv run pytest -q tests/test_carry_paper_engine.py tests/test_carry_v2.py`: 58 passed.
- `git status`: змінені лише `docs/ideas/IDEA-20261102-standing-improvements.md`, `src/tradedesk/collector/carry_paper_engine.py`, два тестові файли; новий `docs/changes/2026-11-12-carry-feed-held-only.md`. `execution/` і `core/` не чіпано.

## Висновки

1. **Поведінка рушія не змінена (ок).** У `carry_paper_engine.py` змінено лише docstring `CarryFeed`. Виклик `feed.load(params, t, rows, held_only=True)` на зупиненій книзі (рядок 409) і сигнатура протоколу (рядок 121) без змін. Вердикт дано за diff.
2. **Новий тест упав би на старому коді (ок).** `test_stopped_a_runs_with_fixture_feed_subclasses` викликає `run_cycle(..., entries_stopped=ENTRIES_STOPPED["A"])`. Рушій тоді передає `held_only=True`, а старі перевизначення `load` у `TruncatedFeed`, `DroppingFeed`, `PermanentHoleFeed`, `IncompleteHoleFeed`, `MidWindowHoleFeed` цього параметра не приймали. Був би `TypeError`, і перевірка `status == "done"` не пройшла б. Додатково `held_only_seen == [True]` доводить, що прапорець дійшов до `FixtureFeed.load` через override.
3. **Картка 346 (ок).** Формат як у 343–345: `- [x] 346. (зроблено 12.11, [зміни](…), джерело … [рев'ю пункту 342](…); без \`execution/\`) Розробник → критик C: …`. Обидва посилання існують. Зауваження 4 у рев'ю 342 справді про це. Кількість «вісім перевизначень» збігається з diff.
4. **Низька, не блокує.** Інлайн-підкласи `Changed`, `Changed4`, `Reported` і `VanishFeed` приймають `held_only`, але новий тест їх не проганяє на зупиненій книзі. Покриті п'ять іменованих класів. Фіксу не вимагаю.
5. **Низька, не блокує.** У `docs/changes/…` написано «57 passed» для пари `carry_paper_engine` + `carry_paper_live`, а запитаний набір дав 58 (з `test_carry_v2`). Це різні набори, розбіжності нема.
