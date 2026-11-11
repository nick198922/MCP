# Розклад витрат carry A: `payment_published` бачить виплати лише до `as_of` (пункт 314)

Джерело: зауваження 5 критика ([рев'ю](../reviews/2026-11-10-carry-a-cost-breakdown.md)). Картка: [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 314.

- Було: `payment_published` (`src/tradedesk/analytics/carry_cost_breakdown.py`) шукала «пізніше збережену» виплату серед усіх `cp_funding.ts`, тож повторний прогін з минулим `--as-of-ms` міг позначити прапорець як `published_later` за даними, яких на той момент ще не було.
- Стало: новий параметр `as_of_ms` (за замовчуванням `None`, поведінка як раніше); якщо його задано, виплати з `ts > as_of_ms` ігноруються. `scripts/verify_carry_a_cost_breakdown.py` передає `as_of` прогону (`--as-of-ms` або момент запуску), тож для звичайного прогону результат не змінюється.
- Тест `test_payment_published_ignores_payments_after_as_of` у `tests/test_carry_cost_breakdown.py`: виплата, збережена після `as_of`, дає `never_stored` з `as_of_ms` і `published_later` без нього.
