**Крок:** p, картка `mcp-cleanup` (Порядок 1), пункт 2: злиття `coinalyze_*` → `perps_*`. Вищі картки: options-playbook п.4 — черга порожня (4.10); agents-mcp-access — питання до власника вже відкрите (`.claude/` заборонено); profit-search п.2 — клас вичерпано; structural-edges 8f — чекає до 2028; x-traders — гілка `x-btc-charlie-sample-registration` чекає власника.

**Результат:**
• `coinalyze_oi_history`, `coinalyze_funding_history`, `coinalyze_long_short_history` → `perps_oi_history`, `perps_funding_history`, `perps_long_short_ratio` з `source="coinalyze"`; 102 → 99 інструментів.
• Числа старих інструментів збережено (тіла перенесено без змін, еталонні тести на `source`); у `perps_funding_history` `days_back` до 365 (понад 180 для біржового джерела — `ok=false`, `invalid_argument`).
• `make lint` і `make test`: 10365 passed, 3 skipped.
• Живу перевірку Coinalyze не зроблено (у середовищі агентів нема ключа).

**Критик:** approve (C), 4 дрібні зауваження; виправлено застарілу назву в LIB-trader_xo і прогрес картки; пороги в тестах та узгодження назв параметрів (`days` / `days_back`) лишено.

**Далі:** `owner`. Власнику: після злиття перезапустити службу MCP; перевірити `perps_funding_history(source="coinalyze", days_back=7)` (раніше 8 добових рядків, останній 3.132e-05); у `CLAUDE.md` «104 шт.» → 99. Лишились три `range_*`.
