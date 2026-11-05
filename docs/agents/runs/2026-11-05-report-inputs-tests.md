**Крок:** p, постійна картка `IDEA-20261102-standing-improvements`: єдиний відкритий пункт 8a чекає даних сервера, тож додано пункт 98 («Як поповнювати»: технічний борг, BACKLOG 6.9).

**Результат:**
• 8 функцій `analytics/`, які годують звіти й форварди, отримали тести (`tests/test_analytics_report_inputs.py`, 20 випадків): `horizon_missing_reasons`, `close_early_payout`, `render_robustness`, `report_keys`, `fmt_dur`, `fmt_ts`, `spread_price_pnl`, `spread_side_cost`.
• Еталони — ручні розрахунки; помилок у коді не знайдено, `src/` не змінено.
• Повторний підрахунок: 105 функцій без тестів (а не 107), тепер 97.
• Дрібниця без виправлення: `fmt_dur` пише «60 хв» замість «1 год» для 3570–3599 с (косметика).

**Критик:** C — approve ([рев'ю](../reviews/2026-11-05-report-inputs-tests.md)); зауваження не блокують (`render_robustness` частково через `startswith`).

**Перевірка:** `make lint` чисто; `make test` — 4664 passed, 3 skipped.

**Далі:** `owner`. Нових питань немає.
