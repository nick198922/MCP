# Рев'ю: tool-failure-tests-7 (пункт 328)

Вердикт: **approve**

Перевірено самостійно: `git diff`, `make lint` (чисто), `uv run pytest -q tests/test_tool_failure_extra_18.py tests/test_rules.py tests/test_store.py tests/test_journal.py` (83 passed).

Знахідки:

1. (info) Інваріант 1 дотримано. `rules_list` пропускає рядок із не-0/1 `enabled` і дає warning. Якщо зіпсовані всі рядки, повертається `ok=false` з `missing`. `rules_disable` не повертає зіпсований рядок.
2. (info) Регресії для валідних даних немає. `_parse_rule_row` приймає лише `type(v) is int` зі значенням 0 або 1. Текст "0"/"1" і 1.0 SQLite через INTEGER affinity зберігає цілими, тому вони лишаються валідними. Тест `test_rules_list_valid_enabled_unchanged` це перевіряє. Рушій алертів вибирає `enabled = 1` у SQL і поле в Python не читає. Решта викликів `rules_list` у `tools/rules.py` (перевірка дубліката за ім'ям, рядок 275) `enabled` не використовують.
3. (info) `RecursionError` у `_parse_journal_row` ловиться, глибоко вкладені tags читаються як порожній список. Тест на 100 000 дужок покриває `positions_list` і `positions_close`.
4. (info) Тести змістовні. Кожен із 9 варіантів зіпсованого `enabled` (текст, 2, -1, 0.5, blob, невалідний UTF-8) тестується окремо, є тести на ремонт перемикачем, на строге читання в store і на новий параметр `flags`. Нечислові tags (NaN/Infinity) зафіксовано окремо.
5. (info) Опис `rules_list` лишається англійською і згадує пропуск зіпсованих рядків.
6. (low, необов'язково) У `_parse_rule_row` рядок `enabled` залишається небулевим значенням у результаті store. Це навмисно, бо його відсікає `drop_corrupt_rows`. Будь-який майбутній виклик `store.rules_list()` повинен використовувати цей фільтр. Про це варто сказати в docstring store, але блокером це не є.
