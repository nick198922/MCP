# Рев'ю: пункт 212, `funding_spread_scan` і неповна історія funding

Вердикт: **approve**. Блокерів немає.

## Що перевірено особисто
- `git diff` по `analytics/carry.py`, `tools/perps.py`, `tests/test_carry.py`, `tests/test_funding_spread_incomplete.py`, `docs/changes/2026-11-07-funding-spread-incomplete.md`.
- `make lint`: чисто.
- `uv run pytest -n0` на `test_funding_spread_incomplete`, `test_carry`, `test_funding_spread_hl`, `test_carry_paper`, `test_tool_desc_audit`: 75 passed.

## Інваріанти
1. Інструмент не кидає виняток. Новий код лише читає `got.get("complete")` і викликає `b.warn`. Збій провайдера йде через `b.guard` у `missing`, і тест `test_funding_history_failure_goes_to_missing` це перевіряє.
2. `spread_snapshot` лишається чистою функцією. Ключ `incomplete` необов'язковий, вхідні дані не мутуються.
3. Опис інструмента англійською. У ньому є `incomplete_history` і оновлене визначення `tradable_now`.
4. На повній історії виходу не змінено. Ключ `incomplete` у ногу не додається, прапора й попередження немає. Це перевіряє `test_complete_history_has_no_flag`.
5. Паперовий carry не зачеплено. `carry_paper.py:304` будує ноги без ключа `incomplete`, тож `spread_snapshot` для нього працює як раніше. Він має власне попередження в рядку 288.
6. Нового інструмента немає, тож README, PARITY і `EXPECTED_TOOLS` не потрібні. Запис у `docs/changes/` є.

## Якість тестів
Тести впали б при мутації коду:
- Прибрати `incomplete_history` з `blocking`: падають аналітичний тест і тести через MCP, бо вони вимагають `tradable_now is False`.
- Не передавати `incomplete` з `perps.py`: падають три тести з прапором.
- Перевіряти обидві ноги пари замість однієї: `test_spread_snapshot_incomplete_history_blocks_tradable` перевіряє і short, і long.
- Неповна нога поза парою дає лише попередження без прапора: перевірено окремим тестом.
- Тести ccxt (bybit) і Hyperliquid — окремі, через патч провайдерів.

Мутації я не запускав, бо правити код мені заборонено. Висновок зроблено з читання асертів.

## Зауваження (не блокують)
1. **Низька.** Попередження додається лише для ноги, яка потрапила в `legs`. Якщо історія неповна і при цьому немає ні записів, ні поточного funding, нога відкидається без попередження. Це вузький випадок, і таку ногу видно як відсутню.
2. **Низька.** У тестах мутується лише `complete`. `truncated` і `dropped_rows` не перевіряються окремо. Для цієї зміни це достатньо, бо читається один сигнал `complete`.
3. **Інформаційна.** Заявлену в `docs/changes` перевірку наживо на сервері я не відтворював. Змінюється лише поведінка за `complete=False`, а на живих даних вона не спрацьовує.
