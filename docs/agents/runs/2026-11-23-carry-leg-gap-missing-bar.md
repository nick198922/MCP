**Крок:** постійна картка `standing-improvements`, п. 383b (його «не раніше 23.11» настало); усі картки власника з відкритими пунктами заблоковані (див. нижче).

**Результат:**
• `carry_leg_gap.classify` тепер рахує рядки без 4h-бару (`no_bar`, `r2_unknown`); без прапорців дефекту вердикт ◯ «R2 unknown», а не мовчазний ✗.
• Скрипт `verify_carry_b_leg_gap.py` друкує лічильник; 2 нові тести, 11 passed.
• `make lint && make test`: 11023 passed, 3 skipped.

**Критик:** approve, блокерів нема ([рев'ю](../../reviews/2026-11-23-carry-leg-gap-missing-bar.md)); дрібне зауваження про нотатку в changes виправлено.

**Заблоковано:**
• options-playbook п. 4 — у черзі правил нема нового; agents-mcp-access — Q-20261122-1 відкрите; structural-edges 8f — чекає даних (≈ 2028); book-walls п. 1 — код уже в `main`, лишилось увімкнення на сервері (Claude); standing-improvements п. 17 / 8a — потрібні дані сервера; x-traders, profit-search — зупинка за правилом / немає кандидаток.

**Далі:** `owner`. Відкриті питання: Q-20261122-1, Q-20261122-2, Q-20261122-3.
