# Рев'ю: поточний funding без NaN / inf (пункт 257), 2026-11-07

Режим C, незакомічений diff + `tests/test_current_funding_nonfinite.py` + `docs/changes/2026-11-07-current-funding-nonfinite.md`.

**Вердикт: approve.** Блокерів немає.

## Що перевірено сам
- `make lint`: чисто.
- Тести: `test_current_funding_nonfinite`, `test_analytics_direct_15`, `test_traders`, `test_review_leftovers_nonfinite`, `test_tools_wrong_type_fields` дали 271 passed. Усі тести з `funding|carry|spread` дали 744 passed, 1 skipped.
- Зміни в `execution/` відсутні. Нових мережевих викликів немає: усе йде через наявні `rt.*` і `b.guard`.

## Інваріанти
1. Інструмент не кидає виняток. `float(x["fundingRate"])` у `perps_funding_compare` міг дати `ValueError` поза `guard`. Тепер `finite_float` лежить у `try/except (TypeError, ValueError)`, а тест `"abc"` перевіряє `_failure(res) is None`.
2. Відкинутий символ чи біржа називаються в `warnings`. У `current_funding` і `hl_current_funding` попередження одне на біржу, з обрізанням до 10 імен. У `perps_funding_compare` попередження на кожну біржу. Тести перевіряють текст.
3. `analytics/` лишається чистою. `option_xvenue.num` лише додає `OverflowError` до перехоплюваних. Тест покриває `10**400`, `-(10**400)` і `10**300 == 1e300`.
4. Зміна в `schema.py` стосується лише коментаря в SQL-міграції. Рядок міграції не змінено, тож хеш чи версію вона не чіпає (це лише текст у списку).

## Зауваження (не блокують)
1. **Низька.** Числовий рядок у масовому funding ccxt тепер розбирається, раніше `json_num` його пропускав. Це змінює поведінку і описано в `docs/changes`, тож ухвалено свідомо. Сюди ж: для Deribit у `perps_funding_compare` `json_num` залишається першим фільтром. Рядок `"abc"` там мовчки пропускається без попередження, тест це фіксує. Для Hyperliquid те саме: `"abc"` провайдер читає як відсутню ставку. Асиметрію між біржами задокументовано в тесті, на цифри вона не впливає.
2. **Низька.** У `hl_current_funding` попередження називає `coin`, а в `perps_funding_compare` мітка містить `base`. Формулювання різні, але обидва містять фразу `not a finite number`.
3. **Низька.** Нове попередження в `perps_funding_compare` потрапляє в `warnings`, але `missing` не отримує запису. Це відповідає наявній поведінці для відсутньої ставки.
4. Тест `tracked_traders` змістовний: він перевіряє і виключення порогом, і те, що з порогом 0 той самий рядок очолює рейтинг. Тож саме поріг прибирає його, а не PnL.

Живий виклик, про який йдеться в changes, я не перевіряв: у хмарному середовищі біржі недоступні.
