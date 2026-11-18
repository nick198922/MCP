**Крок:** постійна картка `standing-improvements`, пункт 382 (додано координатором: усі картки власника заблоковані — очікування даних / часу / власника).
**Результат:**
• `funding_carry_scan`: типова відповідь 14.2 → 5.7 тис. символів (top 10, `summary` по всіх символах, evidence злито).
• `market_markets`: 19.1 → 4.3 тис. (повні 10 рядків + список символів до `limit`).
• Повний вивід — `include_details=true`; тести розміру ≤ 8 тис. у `tests/test_compact_defaults.py`.
**Критик (C):** approve; не блокує: попередження «compact» у `funding_carry_scan` з'являється й без обрізання, README без `include_details`.
**Перевірено мною:** `make lint` чисто, `make test` 10461 passed.
**Заблоковано (для довідки):** agents-mcp-access (правити `.claude/` агентам заборонено), options-playbook п. 4 (черга порожня), x-traders (гілка btc-charlie чекає), profit-search, structural-edges 8f/8k/8l (чекають дат), demo-setups-depth 6, short-setups-demo, mcp-cleanup п. 3 і book-walls п. 1 (лишок — власнику / Claude на сервері).
**Далі:** `owner`.
