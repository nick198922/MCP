**Крок:** p/e — постійна картка `standing-improvements` (Порядок 1) мала один відкритий заблокований пункт (8a), тож додано пункт 179 (BACKLOG 6.9) і виконано; решта карток власника чекає часу, даних чи власника.
**Результат:**
• `tests/test_analytics_direct_8.py`: 13 тестів на 10 функцій `analytics/` (pm_touch, liquidation_flush, funding_crowding, usde_funding, holders_revenue, trader_persistence, markup ×2, market_flow, strategy)
• Лічильник без тестів: 64 → 54 із 2171; `src/` не змінено
• Нотатка: `simple_return` дає `None` при ціні виходу 0 (поведінку зафіксовано, не змінено)
• `make lint` і `make test`: 6591 passed, 3 skipped
**Критик:** C (sonnet) — approve; зауваження низьке: тест `question_direction` на зразку — регресійний, не незалежний ([рев'ю](../../reviews/2026-11-06-analytics-tests-5.md)).
**Далі:** `owner`. Відкрите питання Q-20261106-1 (поріг для «48 год перерви») чекає власника.
