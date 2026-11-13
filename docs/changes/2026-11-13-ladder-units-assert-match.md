# 2026-11-13 · Негативні тести ladder перевіряють, яка саме перевірка впала (пункт 376)

Пункт 376 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md), зауваження 1 [рев'ю пункту 373](../reviews/2026-11-13-ladder-units-negative-tests.md): `pytest.raises(AssertionError)` без `match` доводив лише, що впала якась перевірка. Змінено лише `tests/test_ladder_weekly_units.py`; `src/`, `scripts/`, `deploy/` і `execution/` не чіпав.

- Кожне твердження в `_assert_units` тепер має повідомлення з власною міткою на початку: `exec_start:`, `script:`, `out_dir_count:`, `out_dir_path:`, `on_calendar:`, `persistent:`.
- Кожна з 10 мутацій у `_MUTATIONS` має очікувану мітку, і тест вимагає `pytest.raises(AssertionError, match=r"^<мітка>: ")`. Тест зі зміненим `LADDER_SERVER` вимагає `out_dir_path:`. Мітки збігаються з таблицею в рев'ю пункту 373, тож ручна звірка тепер закріплена в CI.
- Самі перевірки, мутації та позитивний тест не змінено.
- **Перевірка мутацією (тимчасово, не комічу):** заміна `"scripts/paper_ladder_snapshot.py" in argv` на позиційну `argv[-3] == …` дає 2 падіння, `out_dir_removed` і `second_out_dir`: тепер їх ловить `script:` замість `out_dir_count:`. Старий тест без `match` пройшов би обидва випадки, бо `AssertionError` таки виникав. Після відкату: `uv run pytest -q tests/test_ladder_weekly_units.py` дає 12 passed, `make lint` чисто.
