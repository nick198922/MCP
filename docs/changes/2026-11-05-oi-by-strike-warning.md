# 2026-11-05 — `deribit_oi_by_strike`: попередження про опорний форвард лише за справді різних форвардів

Пункт 108 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md); джерело — [рев'ю oi-interval-fixes](../reviews/2026-11-05-oi-interval-fixes.md), зауваження 7.

- **Що змінено.** Попередження «all expiries summed…» (без `expiry`) тепер з'являється лише тоді, коли форварди експірацій відрізняються більш ніж на `FORWARD_REL_TOL = 1e-6` (відносно; `math.isclose` між найменшим і найбільшим форвардом). Раніше умова була точною рівністю чисел з рухомою комою, тож шум в останніх знаках `underlying_price` теж давав зайве попередження.
- **Чому 1e-6.** `distance_pct` округлюється до 0.01 п.п. (1e-4), тож за меншої різниці форвардів опорний форвард дає ті самі `distance_pct` і ту саму смугу.
- **Числа не змінено:** `notional_usd`, смуга `width_pct`, `distance_pct` і поле `forward` рахуються як раніше.
- **Тести** (`tests/test_server.py`): `test_oi_by_strike_reference_forward_warning_uses_tolerance` — різниця 1e-9 → без попередження; 1 б.п. і 1.8 % → попередження. Чинні `test_oi_by_strike_walls` (різні форварди → попередження) і `test_oi_by_strike_no_reference_forward_warning_when_forwards_equal` (однакові → без) пройшли без змін.
- `execution/`, колектори й заморожені форварди не змінено.
