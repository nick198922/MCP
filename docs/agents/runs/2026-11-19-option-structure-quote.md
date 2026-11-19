**Крок:** p (Порядок 1, standing-improvements): усі картки Порядку 0 заблоковані, тож додано й виконано пункт 410 із BACKLOG.
• Заблоковано: options-playbook (черга пункту 4 порожня), agents-mcp-access (правка `.claude/agents/*` заборонена агентам), x-traders (картку змінює відкрита гілка btc_charlie), profit-search (п. 2 без нових даних, п. 3 — фокус), structural-edges 8f (чекає 2028), demo-setups-depth / short-setups-demo (чекають угод), sieve (пауза), book-walls (увімкнення й калібровка — Claude на сервері).

**Результат:** інструмент `option_structure_quote` (read-only): кондор, дебетові спреди, стренгл, стреддл за bid / ask Bybit з комісією; ноги, премія, макс. збиток / прибуток, беззбитки, R:R.
• Використано функції генератора `option_ideas`; додано `long_straddle`, `structure_legs`.
• `make lint` чисто, `make test`: 10831 passed, 3 skipped. Живий виклик розробника (BTC кондор, ETH стреддл) збігся з ручним розрахунком.

**Критик:** C (sonnet) — approve; блокерів нема. Не блокує: фільтр USDC у генераторі, перевірка `min_days <= max_days`, текст попередження про маржу (записано в картці).

**Далі:** `owner` (PR). Після злиття — звірити відповідь через `scripts/mcp_call.py option_structure_quote '{"currency":"BTC"}'`. `CLAUDE.md` каже «104 інструменти» — правка за власником.
