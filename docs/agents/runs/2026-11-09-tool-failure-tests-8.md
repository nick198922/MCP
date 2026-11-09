**Крок:** постійна картка (крок p, пункт 292): останні 7 інструментів зі списку BACKLOG 6.53 без окремого тесту збою; інші картки власника чекають часу чи Claude з чату.

**Результат:**
• `tests/test_tool_failure_extra_9.py`: 108 тестів для `strategy_payoff`, `strategy_straddle`, `strategy_strangle`, `strategy_risk_reversal`, `strategy_iron_condor`, `strategy_funding_carry`, `strategy_scenario_matrix`.
• Знайдено й виправлено: `strategy_payoff` і `strategy_scenario_matrix` відповідали `ok=true` на зіпсовані ноги (невідомий kind/side, size ≤ 0, strike ≤ 0, від'ємна премія); `horizons=[]` давав порожню матрицю; від'ємний `iv` горизонту рахувався як нульовий. Тепер `ok=false` (`invalid_input`), нова чиста функція `leg_problem` в `analytics/strategy.py`.
• Зміна поведінки: від'ємна `entry_premium` тепер відхиляється.
• `make lint` чисто, `make test`: 8091 пройшли, 3 пропущені.

**Критик:** approve без блокерів ([рев'ю](../../reviews/2026-11-09-tool-failure-tests-8.md)); зауваження: `entry` ліній `linear` не перевіряється (кандидат наступного пункту).

**Далі:** `owner` (PR на злиття). Нових питань немає. Відкриті Q-20261106-1 і Q-20261109-1 чекають власника.
