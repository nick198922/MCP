**Крок:** step p/e — усі картки власника заблоковані; поповнено постійну картку пунктом 393 (знахідки рев'ю btc-mempool-tools) і виконано.

**Чому не інше:**
• options-playbook п. 4 — черга порожня (4.11); agents-mcp-access — потребує правки `.claude/*` (Q-20261114-2 open)
• x-traders — гілка чекає власника; profit-search — 4 партії без кандидаток; structural-edges 8f — ◯ до ≈ 2028
• demo-setups-depth, short-setups-demo, book-walls — чекають даних / дати; sieve на паузі

**Результат:**
• 2 тести `fees_summary` (різні значення ключів; відсутній ключ), застереження про наближені смуги в описі `btc_mempool` і `warnings`; логіку не змінено
• `make lint && make test`: 10566 passed, 3 skipped

**Критик:** C (sonnet) — approve; низьке зауваження про формулювання «нижня межа» не блокує.

**Далі:** `owner`. Питань нових немає.
