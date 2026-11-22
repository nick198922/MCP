**Крок:** п. p–e без роботи для Порядку 0–1 (усе заблоковане); взято `onchain-parity` п. 2б (Порядок 2): ключ Etherscan у службі MCP з'явився 22.11, тож жива перевірка розблокована.

**Результат:**
• Усі чотири `onchain_*` відповіли живцем через MCP служби (ethereum): баланс ETH / USDC, перекази, token info, код контракту.
• Безкоштовний ключ: `balance`, `address_txs`, `token_info` — лише ethereum, polygon, arbitrum (bsc, optimism, base, avalanche → `missing: provider_tier`). `onchain_contract_source` працює на всіх 7 мережах.
• Код: `FREE_CHAINS` і `FREE_SOURCE_CHAINS`, описи інструментів не обіцяють недоступного; живий зразок `tests/fixtures/etherscan_v2_live_2026-11-22.json` (без ключа), +6 тестів.
• Сирі тіла Etherscan лишилися з документації: знімати їх можна лише з ключем напряму, а агенти ключів не читають.
• Пункт 2 (2а, 2б) закрито в картці. `make lint` чисто, `make test`: 11018 passed.

**Критик:** C, раунд 1 — request changes (`contract_source` хибно описано як обмежений; покриття мереж у зразку неповне); раунд 2 після виправлень — approve ([рев'ю](../../reviews/2026-11-22-etherscan-live-check.md)).

**Пропущено:** `options-playbook` п. 4 (нема правил), `agents-mcp-access` (правка `.claude` заборонена дозволами агентів, попри відповідь власника), `x-traders` (відкрита гілка змінює картку), `structural-edges` 8f і `book-walls` (час / дані), `profit-search` п. 2 (партія порожня).

**Далі:** `owner`. Нові описи інструментів з'являться в службі після злиття й перезапуску. Питань нема.
