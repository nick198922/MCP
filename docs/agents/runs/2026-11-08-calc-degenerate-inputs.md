**Крок:** p — постійна картка `standing-improvements`, відкритих пунктів менше трьох; додано пункт 271 (калькулятори з виродженими входами, інваріант 1).

**Результат:**
• Усі 10 калькуляторів (`option_calc`, `touch_probability`, `odds_devig`, `fib_retracement`, `strategy_*`) на NaN / inf / `10**400` кидали виняток або віддавали `ok=true` з NaN / Infinity.
• Тепер `ok=false`, причина `invalid_input`, без винятків; звичайні відповіді не змінились (тест з еталоном Black-76).
• Тест `tests/test_calc_degenerate_inputs.py`: 100+ викликів через MCP-клієнт.
• Лишилось у BACKLOG 6.53: `strategy_cash_and_carry` / `strategy_funding_carry` з `"inf"` і від'ємні `days` у `horizons`.

**Критик:** C, approve, блокерів немає ([рев'ю](../../reviews/2026-11-08-calc-degenerate-inputs.md)). `make lint` і `make test` (7422 passed) — мої.

**Далі:** `owner`. Відкрите питання Q-20261106-1 чекає власника.
