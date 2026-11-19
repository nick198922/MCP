**Крок:** p → усі картки «Порядок 0/1» заблоковані; за REFILL додано пункт 392 у `standing-improvements` (застарілий рядок доказів P10/P11 у `RULES`).

**Результат:**
• Пункт 392 виявився дублем: виправлення вже в `main` (коміт `a93648fa`, 18.11), тест `test_p10_p11_carry_the_playbook_status` є. Коду не змінено.
• Пункт закрито в картці з поміткою «дубль». Запис 4.10/4.11 у плейбуку про «розбіжність» застарів.
• `make lint` і `make test` (10500 passed, 3 skipped) — зелені.

**Заблоковано (чому):**
• options-playbook п. 4: у черзі немає правила з історією; agents-mcp-access: Q-20261114-2 (правка `.claude/agents/*` — власнику); x-traders: гілка `agent/2026-11-18-x-btc-charlie-sample-registration` чекає власника.
• profit-search п. 2 — партія 3 уже сьогодні, нових даних нема; п. 3 — пауза FOCUS; п. 4 — accepted-карток нема.
• structural-edges 8f/8k/8l — потужність (2027+); demo-setups-depth п. 6 — звірено 18.11; short-setups, sieve — час / пауза.
• book-walls п. 1 і standing-improvements (1, 8a, 383b до 23.11) — потрібен сервер / Claude; assistant — підтвердження власника.

**Критик:** не запускався (змін коду нема).

**Далі:** `owner` (лише docs). Нових питань нема.
