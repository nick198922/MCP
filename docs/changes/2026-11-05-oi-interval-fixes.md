# 2026-11-05 — `detect_interval_hours` і `oi_by_strike`: дві розбіжності з BACKLOG 6.12

Пункт 73 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), [BACKLOG 6.12](../BACKLOG.md).

- **`derivatives.detect_interval_hours`: виправлено лише docstring.** Функція повертає сиру медіану кроку між мітками (7.5 год → 7.5), а для менш ніж двох різних міток або кроку < 15 хв бере `fallback`. Округлювати до {1, 2, 4, 8} год не стали: значення йде в `carry.carry_snapshot` (`coverage_days`, отже APR нових лістингів у paper carry та `tools/carry_paper.py`) і в `summarise_funding_history` (span для `carry_annualized` у `tools/perps.py`). Округлення змінило б числа форвардів, тож поведінку не змінено. Тест `test_detect_interval_hours_returns_raw_median_not_rounded`.
- **`options.oi_by_strike`: `notional_usd` тепер дорівнює сумі OI × форвард кожного рядка** (раніше бралися OI страйка × форвард першого рядка). У `deribit_oi_by_strike` без `expiry` ноцінал кожної експірації тепер рахується за її власним форвардом. Для однієї експірації числа не змінились. Тест `test_oi_by_strike_notional_uses_each_rows_own_forward` (форварди 100 і 110: 3050, а не 3000, незалежно від порядку рядків).
- **`deribit_oi_by_strike`:** в описі й у `warnings` (коли експірацій більше однієї і `expiry` не задано) сказано, що смуга `width_pct`, `distance_pct` і поле `forward` беруть один опорний форвард (форвард першого опціону з відповіді Deribit, не обов'язково найближчої експірації). Цю поведінку не змінено. Перевірка в `tests/test_server.py::test_oi_by_strike_walls`.
- Тести пункту 72 лишились чинними без змін: у них усі рядки мають однаковий форвард.
- `execution/`, колектори, `core/`, `providers/` і заморожені форварди не змінено.
