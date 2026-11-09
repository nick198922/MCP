# Рев'ю: тести збою, пункт 289 (plan_link, rules_test, odds_devig, fib_retracement, option_calc, strategy_cash_and_carry)

Режим C. Вердикт: **approve**.

Перевірено мною: `make lint` чисто; `uv run pytest -q tests/test_tool_failure_extra_7.py tests/test_rules.py tests/test_trade_plan.py` дає 133 passed. Прочитано повний `git diff -- src` і список тверджень у новому тест-файлі.

## Інваріанти
1. Інваріант 1 (інструмент не кидає назовні). Виправлено три справжні діри:
   - `plan_link`: id > 2^63-1 давав OverflowError. Тепер це `invalid_input` з "not found".
   - `plan_link`: зіпсований рядок плану після запису давав виняток у `_view`, а inf чи NaN йшли у відповідь. Тепер рядок проходить `_listable`, а при збої повертається `{id, journal_id}` з попередженням. Лінк записано, бо він не читає збережених рівнів.
   - `rules_test`: у відповіді з `ok=true` могли бути Infinity чи NaN, а збій обчислення теж давав `ok=true`. Тепер `ok=false` з записом у `missing`. `STORE_ERRORS` пробрасується до `store_failed`, тож збій сховища не маскується під помилку eval.
   - `fib_retracement`: гілка `applicable=false` не перевіряла нескінченні значення в драбині. Тепер вона йде через `reject_nonfinite_result`.
   - `_listable` тепер відкидає плани з NaN чи Infinity у `tags` або `rules`. Це зачіпає і `plan_list`, для нього є тест.
2. Інваріант 4 (одиниці): змін немає.
3. Інваріант 5: нових інструментів немає. Опис `rules_test` доповнено англійською, формулювання коректне. `docs/changes/2026-11-08-tool-failure-tests-7.md` є.
4. Happy path не змінено. Тести `test_plan_link_extreme_but_valid_plan_unchanged`, `test_rules_test_extreme_but_valid_values_unchanged` і `test_calculators_extreme_but_finite_inputs_answer` фіксують, що екстремальні, але валідні входи (1e307, 1M символів, 100k елементів) відповідають як раніше. Наявні `test_rules.py` і `test_trade_plan.py` зелені.

## Якість тестів
Тести не тавтологічні. Кожен перевіряє конкретну пару `ok`/`data`/`missing` (kind і reason) і побічний ефект: `_linked(rt)` показує, що лінк записаний або не записаний. Для збоїв сховища та аналітики використано monkeypatch з параметризацією, для зіпсованих рядків прямий SQL у БД. Є наскрізна перевірка на відсутність нескінченних значень у `data` через `first_nonfinite` та `is_error`.

## Зауваження (низька серйозність, без блокування)
1. `rules_test`: `ok=false` при збої eval є зміною поведінки. Раніше було `ok=true` з `evaluated=false`. Це відповідає семантиці інваріанта 1 (немає даних → `ok=false`), і наявні тести це приймають. Бажано одним рядком зазначити зміну в `docs/changes/` для споживачів, які читали `evaluated`.
2. `plan_link`: попередження від `_listable(Builder(), ...)` відкидаються, бо використано тимчасовий Builder. Причина (рядок зіпсований) все одно названа власним `b.warn`. Це прийнятно.
3. Рядок `rules.py`, що додає `b.miss("value", ...)`, довгий. Ruff format його пропускає, тож лише косметика.

Блокерів немає.
