**Крок:** p — стала картка `IDEA-20261102-standing-improvements`, додано пункт 274 (єдиний відкритий 8a заблокований даними сервера).

**Результат:**
• `strategy_cash_and_carry` / `strategy_funding_carry`: inf / NaN / крайні входи → `ok=false` (`invalid_input`), без Infinity у відповіді.
• `strategy_scenario_matrix`: від'ємні `days` у `horizons` відхиляються (`horizons.N.days`), нуль дозволений.
• `pm_calibration.lag_profile` / `jump_proximity`: зіпсовані точки `/prices-history` пропускаються (без `warnings`: чисті функції).
• 11 нових тестів; `make lint` чистий, `make test`: 7435 passed, 3 skipped.

**Критик:** approve (C), блокерів немає, [рев'ю](../../reviews/2026-11-08-calc-degenerate-inputs-2.md).

**Далі:** `owner`. Лишилось у BACKLOG 6.53: `smart_money`, `collector/` (`parse_bar`, DVOL), `pm_threshold.history_alignment`, `iv ≤ 0` у `horizons`.
