**Крок:** p → картка `standing-improvements`, новий пункт 395 (звірка описів інструментів з кодом); інші картки власника заблоковані.
**Результат:**
• `perps_open_interest` — опис правильний; `ohlcv_history` — виправлено речення про `first_ts_ms` / `last_ts_ms`; `positions_list` — додано два обмеження фільтра `tag`. Логіку не змінено.
• Нові тести: `tests/test_tool_descriptions_units.py` (6).
• Знахідка: фільтр `tag` не знаходить кириличні теги, а `%` і `_` працюють як маски. Додано пункт 396a.
• `make lint` і `make test` зелені: 10607 passed.
**Критик:** C (sonnet) — approve; зауваження про пункт 396a враховано.
**Заблоковано, не бралось:**
• `agents-mcp-access` — запис у `.claude/` заборонено, чекає Q-20261114-2.
• `options-playbook` п. 4 — черга правил «не перевірено» порожня.
• `profit-search` п. 2 — нових даних нема (партії 1–4).
• `structural-edges-depth` 8f — чекає вікна до 2027-02-24.
• `book-walls` — чекає увімкнення на сервері й даних.
• `x-traders` — картку змінює чекаюча гілка `agent/2026-11-18-x-btc-charlie-sample-registration`.
**Далі:** `owner`.
