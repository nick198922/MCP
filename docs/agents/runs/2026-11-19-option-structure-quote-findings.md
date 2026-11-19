**Крок:** step p/e — усі картки власника заблоковані (options-playbook п. 4: черга порожня; x-traders: чекає гілка; structural 8f: до 2028; book-walls, onchain-parity, demo-setups: сервер / ключ / дата). Взято поповнення standing-improvements: п. 411 (розробник → критик C).

**Результат:**
• `parse_bybit_tickers` читає лише `-USDT`: генератор і `option_structure_quote` бачать однакові котирування.
• Нерівні крила кондора: `wing_widths` у `data` + warning.
• `prob_profit` стреддла / стренгла = null, якщо беззбиток поза сіткою путів.
• `min_days > max_days` → `missing` з причиною; тест ETH; попередження про маржу в двох формулюваннях (для дебетових спредів лишено — за базою знань продана нога потребує маржі).
• `make lint` і `make test` зелені (10836 passed).

**Критик:** C (sonnet) — approve, 2 низькі зауваження (зміна `prob_profit` діє й на тижневий генератор; сувора межа сітки).

**Далі:** `owner`. Не заблоковані власником: Q-20261119-1 (ключ Etherscan), Q-20261114-2 (доступ агентів до MCP). Варто перевірити після деплою `option_structure_quote` живцем через `scripts/mcp_call.py`.
