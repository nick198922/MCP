**Крок:** p → стала картка `standing-improvements` (Порядок 1): інші картки власника заблоковані, тож додано пункт 412 (поповнення).
**Результат:**
• Описи `system_health` і `polymarket_search` та README (`cycle_regime`, `polymarket_search`, `market_tickers`, `system_health`) уточнено: компактна відповідь, `include_details`, `count` проти `shown`.
• Логіку не змінено; 4 нові тести; `make lint` чисто, `make test`: 10840 passed, 3 skipped.
**Критик:** C (sonnet) — approve; зауваження не блокують.
**Заблоковано / пропущено:**
• options-playbook п. 4: нема нового правила в черзі (4.12).
• x-traders: картку змінює гілка `agent/2026-11-18-x-btc-charlie-sample-registration`, що чекає власника.
• structural-edges 8f: ◯ до ≈ 2028; book-walls: чекає ввімкнення й даних; agents-mcp-access: правка `.claude/agents/*` і `nightly.md` заборонена правилами координатора, потрібне рішення власника.
**Далі:** `owner`.
