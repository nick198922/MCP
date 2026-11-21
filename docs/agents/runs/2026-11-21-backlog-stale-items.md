**Крок:** p/e: усі картки власника заблоковані; поповнено постійну картку (пункт 444, джерело «прибирання документів») і виконано.

**Результат:**
• `docs/BACKLOG.md`: повтор перевірки порога R:R (20.11, ◯ inconclusive) позначено зробленим; ще 4 застарілі пункти закрито з посиланнями (девіація в тижневий тренд, серія сетапів курсу, Trader_XO ✗, carry B BIRB/INIT).
• 8 сумнівних лишено відкритими й названо в `docs/changes/2026-11-21-backlog-stale-items.md`.
• `make lint` і `make test`: 11009 passed, 3 skipped.

**Критик (C, Sonnet):** approve; усі п'ять відміток підтверджені джерелами ([рев'ю](../../reviews/2026-11-21-backlog-stale-items.md)).

**Заблоковано (чому):**
• options-playbook: черга пункту 4 порожня.
• agents-mcp-access: Q-20261114-2 (правка `.claude/agents/*` заборонена).
• x-traders: картку змінює гілка `agent/2026-11-18-x-btc-charlie-sample-registration`.
• profit-search п. 2: кандидаток нема (партія 11, сьогодні).
• structural-edges-depth 8f/8k/8l: чекають дат.
• demo-setups-depth п. 6: жоден нерозібраний сетап не має ≥ 30 угод (S14 28, S39 23).
• book-walls, assistant, onchain-parity: сервер чи ключ власника.
• sieve на паузі; short-setups-demo чекає часу.

**Далі:** `owner`.
