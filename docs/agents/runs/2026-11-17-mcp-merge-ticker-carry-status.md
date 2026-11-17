**Крок:** p, картка mcp-cleanup (Порядок 1), пункт 2 «А1, злиття» — два перші злиття. Картки Порядку 0 заблоковані: agents-mcp-access — питанням Q-20261114-2; x-traders «заклики» — у клоні нема архіву `docs/library/courses/x-*`; options-playbook п. 4 — черга правил порожня; 8f/8k/8l, short-setups — часом; standing-improvements і demo-setups-depth — змінює непрохідна гілка `agent/2026-11-17-audit-weekly`.

**Результат:**
• `market_ticker` злито в `market_tickers` (`symbols`: рядок або список; один символ іде через `fetch_ticker`); рядок тепер у `data.tickers[0]`
• `carry_paper_status` злито в `paper_carry_status(strategy="manual")`; `id` з A/B → `invalid_input`
• Інструментів 104 → 102; таблиця змін старе → нове в `docs/changes/2026-11-17-mcp-merge-ticker-carry-status.md`
• Лишилось у пункті 2: три `coinalyze_*` → `perps_*`, три `range_*` → один (картка лишається відкритою)

**Критик:** C — request changes (1 блокер: `docs/knowledge/forex.md` посилався на `market_ticker`; 1 прогалина тесту batch-шляху). Обидва виправив сам (рядок у доках, випадок у `test_tools_malformed_200.py`); повторного рев'ю критика не було. Живу перевірку біржових чисел (у change-записі) зробив будівельник, не критик.

**Далі:** `owner`; `make lint` чисто, `make test` — 10310 passed, 3 skipped. Нових питань немає. `CLAUDE.md` досі каже «109 інструментів» (зараз 102) — для власника.
