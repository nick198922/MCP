**Крок:** p/standing — картки власника заблоковані (плейбук п. 4: черга порожня; agents-mcp-access: Q-20261114-2; x-traders: гілка btc-charlie чекає; profit-search, 8f/8k/8l, short-setups, demo-setups п. 6: потужність/час; mcp-cleanup п. 3: лишок за власником; book-walls п. 2: не раніше 15.12). Відкритих пунктів постійної картки було два (обидва чекають даних сервера), тож додано пункт 380 з BACKLOG.
**Результат:**
• `market_flow` типово ≈ 7 тис. символів замість 68 тис. (`include_series=false`), `hyperliquid_funding_history` ≈ 0.9 тис. замість 33 тис. (новий `include_series`).
• Тест розміру ≤ 8000 у `tests/test_compact_defaults.py`; споживачі (знімок асистента, панель) передають параметр явно.
**Критик:** C (sonnet) — approve; зауваження без блокування: тест на NaN у `entries` за `include_series=true`, малий запас розміру `market_flow` (7437/8000), рядок про `include_series` у README.
**Перевірено мною:** `make lint` чисто; `make test` 10368 passed, 1 падіння (дубль номера 379 у постійній картці) — виправлено перейменуванням на 380, `test_idea_cards.py` 6 passed.
**Далі:** `owner`. Наступний крок цього пункту — `funding_spread_scan` (BACKLOG).
