# Рев'ю: тести збою інструментів, частина 3 (пункт 280)

Режим C, незакомічена зміна. Вердикт: **approve**.

## Що перевірено сам
- `make lint`: чисто.
- Тести: `test_tool_failure_extra_3.py`, `test_history.py`, `test_trade_plan.py`, `test_journal.py`, `test_positions.py`, `test_ohlcv_hist.py`, `test_tools_never_raise.py`, `test_tool_failure_extra_2.py`: 148 passed. Повний набір запустить координатор.
- Diff: змінено лише `tools/` (`_util`, `history`, `journal`, `positions`, `trade_plan`) і docs. Жодних змін у `execution/`, провайдерах, `analytics/`. Нових інструментів немає, тож чекліст нового інструмента не потрібен. Є запис у `docs/changes/`.
- Інваріант 1 (інструмент не кидає): `drop_corrupt_rows` не кидає. `store_num` відсікає bool, рядки, blob, inf, надвеликі int. У `plan_list`, `journal_stats` і `positions_*` виклик стоїть усередині `try ... STORE_ERRORS`. У `iv_rank` `min`/`max` тепер працюють лише з відфільтрованими числами.
- Валідні рядки проходять без змін: `drop_corrupt_rows` повертає той самий об'єкт `row`. NULL у `numeric` дозволений, обов'язкові лише `required`. У `iv_rank` NULL `atm_iv` пропускається мовчки, як і раніше. Тести `valid_bars_unchanged_without_a_warning` та `null_r_multiple_is_not_corrupt` це фіксують.
- Одиниці не змінені. `latest_ts` в `iv_rank` береться з того самого рядка, що й `latest`.
- Тести справді покривають виправлення. Кожен інструмент має тест збою читання, параметризований набір пошкоджень (рядок, два blob, inf) і окремий випадок «усі рядки зіпсовані → ok=false, а не порожньо». Для `journal_stats`, `plan_list`, `iv_rank` і `alerts_recent` коментарі в тестах називають, що раніше падало.

## Зауваження
1. **Низька, обсяг.** `positions_risk` отримав ще й `reject_nonfinite` для вхідних аргументів та `reject_nonfinite_result` для результату. Це більше за «пошкоджені рядки». Зміна обґрунтована й покрита тестами (`non_finite_input_is_invalid`, `overflowing_result_is_not_ok`), але її варто було згадати в `docs/changes/` окремим рядком. Виправляти код не треба.
2. **Низька.** Після відкидання рядків `positions_list`, `plan_list` і `alerts_recent` можуть повернути менше за `limit`. Попередження називає кількість пропущених, тож це прийнятно. Для `positions_list` і `alerts_recent` `count` показує вже відфільтровану кількість.
3. **Інформаційна.** Для `drop_corrupt_rows` немає прямого юніт-тесту. Покриття лише через MCP-виклики. Для хелпера на 20 рядків цього достатньо.
4. **Інформаційна.** У `_listable` перевірка `entry <= 0` дублює `plan_add`, а `plan_add` її гарантує. Це лише захист від сторонніх записів, і він доречний.

Блокерів немає.
