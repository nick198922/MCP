**Крок:** постійна картка `standing-improvements` (крок p: усі картки з пріоритетом власника заблоковані; поповнено з BACKLOG).
**Результат:**
• Описи 12 MCP-інструментів-близнюків (`iv_rank` / `deribit_vrp` / `deribit_dvol`, `market_ohlcv` / `ohlcv_history` / `hyperliquid_candles`, `liquidations_*`, `rules_disable`, `perps_open_interest`, `positions_list`, `events_list`): речення «коли брати саме його» й одиниці; лише тексти, поведінка не змінена.
• `make lint` чистий, `make test`: 10492 passed, 3 skipped.
**Критик:** C, approve, блокерів нема ([рев'ю](../../reviews/2026-11-19-twin-tool-descriptions.md)); не звірено дрібне: одиниці `perps_open_interest`, `ohlcv_history`, `positions_list`.
**Заблоковано (чому інші картки не взято):**
• options-playbook п. 4 — у черзі плейбуку нема правила; agents-mcp-access — Q-20261114-2; x-traders п. 2 — квартали Trader_XO / KillaXBT вичерпано, docXBT / btc_charlie чекають питань; profit-search, structural-edges 8f (дані до 2028), demo-setups-depth п. 6, short-setups, book-walls — чекають даних, часу або сервера; mcp-cleanup — лишок за власником.
**Далі:** `owner`.
