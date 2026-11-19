**Крок:** p (картки власника) → поповнення `standing-improvements` (пункт 384, розробник → критик C); решта карток заблокована.

**Результат:**
• `onchain_token_info`: попередження, коли symbol і decimals не читаються (адреса не ERC-20); збої вузла (`backend_error`, `rate_limited`) його не дають. `onchain_balance` і повтор на `rate_limited` уже були в `main`.
• Тести без мережі (4 випадки); `make lint` і `make test`: 10842 passed, 3 skipped.
• Без живої перевірки: у службі MCP нема ключа Etherscan.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-19-onchain-token-info-warning.md).

**Пропущено:**
• options-playbook п. 4: у черзі плейбуку нема правила (4.10–4.12).
• agents-mcp-access: потрібна правка `.claude/*` — Q-20261114-2.
• demo-setups-depth п. 6: 19.11 жоден нововерифікований сетап не має ≥ 30 закритих (S14 24, S39 18); додано рядок підрахунку.
• structural-edges 8f (до 2028), book-walls (чекає даних, 15.12), x-traders (гілка btc_charlie чекає), sieve (пауза), profit-search 2–4 (нових даних нема).

**Далі:** `owner`.
