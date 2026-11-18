# Рев'ю (режим C): стисла типова відповідь `funding_spread_scan` (пункт 381)

Дата: 2026-11-18. Дифф: `src/tradedesk/tools/perps.py`, `tests/test_compact_defaults.py`, `tests/test_funding_spread_hl.py`, `tests/test_server.py`, `tests/test_tools_envelope_shape.py`, `docs/changes/2026-11-18-compact-funding-spread-scan.md`, картка 381.

## Вердикт: approve

Перевірено сам:
- `make lint`: чисто.
- `tests/test_compact_defaults.py`, `tests/test_funding_spread_hl.py`, `tests/test_tools_envelope_shape.py`: 14 passed.
- Додатково тести, що викликають `funding_spread_scan` за замовчуванням (`test_review_leftovers_nonfinite`, `test_tool_failure_extra_14`, `test_review_leftovers_2`, `test_live_float_guard_2`, `test_current_funding_nonfinite`, `test_funding_spread_incomplete`, `test_server`): 422 passed.
- Grep: у `src/` і `scripts/` інших споживачів поля `venues` немає (у `store/schema.py` лише коментар).

## Інваріанти
- Інструмент не кидає: нова гілка працює після збору рядків; шлях `ok=false` (`return b.build(None, ok=False)`) іде раніше і не змінений. `_merge_evidence` працює на вже зібраному списку, помилок провайдера не створює.
- Збої досягають `missing`: `missing` не чіпається. Тест `..._compact_still_reports_a_provider_failure` перевіряє точне поле `funding_history:bybit:TIA/USDT:USDT`, `scanned == 19`, і що `ok` лишається true.
- Evidence не вводить в оману: об'єднання по (провайдер, виклик); спільні аргументи лишаються, відмінні (наприклад `symbol`) прибираються, додаються `fetches`, `symbols`, `last_fetched_at`; `fetched_at` = найраніший (консервативно для свіжості), `cached` = лише якщо всі з кешу, затримка = максимум. Поодинокі записи не змінюються. Evidence успішних запитів, збої йдуть у `missing`, тож нічого не ховається.
- Числа й одиниці: рядки компактної відповіді дорівнюють top-10 повних без ключа `venues` (тест порівнює повну рівність), `summary` рахується до обрізання по всіх базах і ідентичний (тест). Одиниці не чіпаються.
- READ_ONLY і опис англійською: анотація не змінена; опис інструмента й `Field` для `include_details` англійською, з одиницями/поведінкою; попередження «compact answer: top N of M rows …» каже, що відрізано.
- Нового інструмента немає, тож `EXPECTED_TOOLS`/README/PARITY не потребують змін; є запис у `docs/changes/`.

## Зауваження (усі низькі, не блокують)
1. Низька. Попередження «compact answer: top N of M rows» з'являється і коли M ≤ 10, хоча рядки не відрізано (відрізається лише `venues` і evidence). Текст ще й правдивий щодо `venues`, тож не хибний; можна було б уточнити. Виправлення: необов'язково.
2. Низька, відоме. З чотирма біржами типова відповідь ≈ 8.4 тис. символів, тест-ліміт 8 тис. перевіряє лише 2 біржі на фейку (≈ 6.6 тис.). Це чесно описано в записі змін; опис інструмента не обіцяє цифру. Виправлення: за бажання, окремим кроком, скоротити число полів рядка або top-N при ≥ 4 біржах; для цього кроку достатньо.
3. Низька. Після злиття втрачається розбивка evidence по символах (лише кількість). Для відтворюваності є `include_details=true`; задокументовано в описі.
4. Низька. У `tests/test_tools_envelope_shape.py` `ARGS` для `funding_spread_scan` тепер `include_details=True`: тести на нечислові значення бачать `venues`, це коректно. Типовий (компактний) шлях ці тести не покривають, але його покриває тест компактності й 7 суміжних файлів вище.

Блокерів немає.
