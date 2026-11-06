# `plan_review` / `plan_resolve` не падають на сирому `expires_ts` (пункт 187, BACKLOG 6.49)

- `analytics/trade_plan.py`: `usable_expires` (число → int unix-секунди, інакше `None`: текст, байти, `bool`, NaN). `resolve_plan` на такому плані не кидає виняток, а повертає `status="open"` з `invalid="expires_ts"` без заповнення. Для звичайних планів результат той самий.
- `tools/trade_plan.py`: `review_open` пропускає відкритий план із сирим `expires_ts` і додає в `warnings` «plan N skipped: stored expires_ts is not a number»; решта планів обробляється як раніше. Перевірка «stale» теж пропускає такі плани. Навмисно `warnings`, а не `missing`: задача `plan_resolve` кидає виняток на `missing`, і один зіпсований рядок валив би її щогодини.
- Валідацію `plan_add`, `execution/` і колектори не чіпали; через `plan_add` таке значення й далі не потрапляє в базу.
- Тести (`tests/test_trade_plan.py`): `test_review_skips_a_stored_raw_expires_and_processes_the_rest` (сирий план 1 + нормальний 2: 2 розв'язано з тим самим R, 1 лишився відкритим, є попередження, `collect_plan_resolve` не падає); `test_resolve_plan_leaves_a_raw_expires_open`.
- Перевірка: `make lint`; `tests/test_trade_plan.py` + `tests/test_server.py` — 99 passed.
