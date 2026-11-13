# Рев'ю: негативні тести unit-файлів ladder (пункт 373)

Режим C. Вердикт: **approve**.

## Що перевірив сам
- `git status` і `git diff`: змінені лише `tests/test_ladder_weekly_units.py`, `docs/ideas/IDEA-20261102-standing-improvements.md` (рядок 373) і новий `docs/changes/2026-11-13-ladder-units-negative-tests.md`. У `src/`, `scripts/`, `deploy/`, `execution/` змін нема.
- `uv run pytest -q tests/test_ladder_weekly_units.py`: 12 passed. `make lint`: чисто.
- Кожну з 10 мутацій зіставив з реальними файлами `deploy/user-units/tradedesk-ladder-weekly.{service,timer}` і з рядками `_assert_units`.

## Чи мутація справжня і яке твердження падає
Захист від no-op: `assert text.count(old) == 1` стоїть до `pytest.raises`, тож зміна unit-файлу, яка зробила б мутацію порожньою, валить тест, а не проходить мовчки.

| Випадок | Падає твердження |
|---|---|
| out_dir_path_changed (`ladder2`) | `out_dir == ladder_server` (передбачене) |
| out_dir_removed | `argv.count("--out-dir") == 1` |
| second_out_dir | `argv.count("--out-dir") == 1` |
| script_renamed | `"scripts/paper_ladder_snapshot.py" in argv` |
| exec_start_commented_out | `len(exec_start) == 1` (0 збігів) |
| second_exec_start | `len(exec_start) == 1` (2 збіги) |
| on_calendar_saturday, on_calendar_utc | перевірка `OnCalendar` |
| persistent_false, persistent_removed | перевірка `Persistent` |
| зміна `LADDER_SERVER` на `-v2` | `out_dir == ladder_server` |

Кожен випадок валиться саме на призначеному твердженні, випадкових падінь (наприклад `IndexError` чи `ValueError`) не бачу: у `out_dir_removed` `--out-dir` зникає цілком, але падіння настає раніше за `argv.index`.

## Зауваження (не блокують)
1. `pytest.raises(AssertionError)` без `match`: тест не доводить, яке саме твердження впало. Це лишається на ручній перевірці за таблицею вище. Якщо хтось пізніше змінить `_assert_units`, порядок тверджень може зсунути причину падіння непомітно. Можна додати `match=` або повідомлення в `assert`. Низька серйозність.
2. `_OUT`, `_EXEC`, `_CAL` дублюють літерали з unit-файлів; це свідомо, бо `count == 1` одразу покаже розбіжність після правки unit-файлу. Дії не потрібні.

Інваріанти CLAUDE.md не зачеплені (немає коду інструментів, мережі, ордерів). Запис у `docs/changes/` є.
