# Рев'ю C: cutoff-відтворюваність звірки демо (пункт 420)

Вердикт: **approve**

Перевірено сам: `git diff`, `git status --short`, `make lint` (чисто), `uv run pytest -q tests/test_demo_shadow.py tests/test_verify_s38_demo_vs_passport.py` (35 passed), grep усіх викликів `match_rows` / `summary` / `not_executed`.

## Інваріанти

- `analytics/demo_shadow.py` лишається чистим: без I/O, час параметром (`now_ms`). Нова `_resolved_by` лише порівнює поля рядка.
- Одиниці правильні: `trade_plan.resolved_ts` у секундах (`analytics/trade_plan.py:126`: `ts_ms // 1000`; `store/schema.py`), а `now_ms` у мс. У коді `ts * 1000 <= now_ms`. Межа `resolved_ts == now` дає «resolved», це покрито тестом.
- `execution/` не змінено, змінені файли тільки з переліку завдання. Скрипти лише читають БД.
- Інші виклики не змінили поведінки. `scripts/demo_vs_backtest.py:435` передає поточний час, а `resolved_ts` реального плану не пізніший за нього, тож статус той самий. Панель (`dashboard.py`) читає готовий JSON. `verify_s3_demo_vs_passport.py` не змінено. Він бере `now_ms` з JSON, тож пояснення в changes слушне. План `resolved` без `resolved_ts` лишається resolved, як і раніше, тож стара поведінка збережена.
- Зауваження 2 (`at_cutoff`, closed без `closed_ts`) виправлено: `was_open` знову вимагає `closed_ts is not None`, випадок додано в тест із поясненням.

## Тести

- `test_plans_resolved_after_now_count_as_open` змістовний: стан на cutoff і пізніший стан дають однакові `not_executed` при `now_ms = cutoff`. Також перевірено пізніший `now`, межу за секундою і план без `resolved_ts`.
- `test_shadow_plans_repeat_at_the_cutoff_whatever_the_run_time` — два прогони з різним `now`, між ними план закривається після cutoff. Звіти збігаються, а з пізнішим cutoff план рахується закритим. Заявлено, що тест падає при поверненні `now`. Я цього не перевіряв.

## Зауваження (не блокують)

1. (низька) Зміну в `verify_s12_demo_vs_passport.py` тестом не покрито. Вона ідентична s38, а s12-скрипт використовує ту саму `demo_shadow`, тож ризик малий. Якщо в нього є тест `run`, додати такий самий випадок.
2. (низька) Заголовок розділу змінився на «… на cutoff». Якщо щось парсить заголовок «Невиконані сигнали … проти тіньових планів», перевірити. Grep по тестах ці скрипти не виявив, ломки немає.
3. (інформаційна) Обмеження про missed без часу зміни в changes описане чесно, повного відтворення для цього випадку немає.
