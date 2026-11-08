**Крок:** постійна картка (покращення), новий пункт 174; пріоритетні картки чекають часу, власника чи сервера.

**Результат:**
• `perps_funding_compare`, `perps_funding_history`, `perps_open_interest`, `perps_basis_compare` додають `warnings`, коли біржа лише в `missing`, а дані є.
• Числа, `evidence`, `missing`, `ok` не змінено; `KNOWN` у тесті форми Envelope порожній.
• BACKLOG 6.47 закрито; друга частина (фейки для 24 інструментів) винесена в 6.48.
• `make lint` і `make test`: 6237 passed, 3 skipped.

**Критик:** C (sonnet) — approve ([рев'ю](../../reviews/2026-11-06-perps-partial-warning.md)); дрібне: імена в попередженні беруться з `missing[].provider`.

**Далі:** `owner`. Відкрите питання Q-20261106-1 (поріг «великого збитку») лишається без відповіді.
