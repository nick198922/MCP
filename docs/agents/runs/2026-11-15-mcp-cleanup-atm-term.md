**Крок:** p, картка mcp-cleanup (Порядок 1), п. 2 — перше зі злиттів `deribit_atm_term` → `deribit_skew_term`. Картки «Порядок 0» заблоковані: плейбук (черга порожня), x-traders (XO чекає дозавантаження архіву; docXBT / btc_charlie — відкриті питання), profit-search (класи джерел вичерпано), 8f (дані до 2028), demo-setups (чекає ≥ 30 угод), сито (пауза), agents-mcp-access (Q-20261114-2).
**Результат:**
• `deribit_skew_term(view="atm")` повертає ті самі ключі й числа, що старий інструмент (еталони на 4 викликах збігаються); вид за замовчуванням не змінено.
• Виклики в `option_ideas_tg.py`, `assistant_snapshot.py`, `live_smoke.py`, промптах і плейбуці переведено; інструментів 105 → 104.
• П. 2 лишається відкритим: ще `coinalyze_*`, `market_ticker`, `carry_paper_status`, `range_*` (запис у BACKLOG).
**Критик:** C (sonnet) — approve, 3 дрібні зауваження без блокерів ([рев'ю](../../reviews/2026-11-15-mcp-cleanup-atm-term.md)).
**Перевірка:** make lint і make test — пройшли (10285 passed, 3 skipped).
**Далі:** owner. Чат-сервіс MCP матиме стару назву до перезапуску після мерджу.
