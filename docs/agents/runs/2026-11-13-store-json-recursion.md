**Крок:** постійна картка, п. 355 (усі картки власника заблоковані чи вичерпані: п. 2/3 profit-search без нових даних, 8f/8k/8l чекають дат, демо-пункти чекають угод або власника).
**Результат:**
• `store/db.py`: `_parse_carry_paper_row` і `_parse_json_cols` тепер ловлять `RecursionError` (дуже глибоко вкладений JSON у комірці раніше валив `carry_paper_*`, `plan_list`, `plan_review`, `plan_resolve`).
• 10 нових тестів (`tests/test_tool_failure_extra_22.py`); `make lint` і `make test` — 9798 passed, 3 skipped.
**Критик:** C — approve ([рев'ю](../../reviews/2026-11-13-store-json-recursion.md)); неблокуючі: ще кілька `json.loads` у db.py без `RecursionError`, у зміні слово «payload» трохи ширше за факт.
**Далі:** `owner`. Відкрито п. 356 у картці (серіалізація глибоких `tags` у `plan_list` / `carry_paper_status`, інші `json.loads`).
