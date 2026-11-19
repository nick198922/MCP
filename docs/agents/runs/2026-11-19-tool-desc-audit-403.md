**Крок:** p → картка `standing-improvements` (Порядок 1); усі картки Порядку 0 заблоковані, пункт додано з «Як поповнювати» (рев'ю «не блокує»).
**Результат:**
• Пункт 403: описи `perps_open_interest`, `ohlcv_history`, `positions_list` звірено з кодом — розбіжностей нема, `src/` не змінено.
• Додано тест `test_unit_lines_checked_against_the_code` (замок на одиниці: oi_base×last, обсяг у базовому активі, одиниці греків).
• `make lint` чисто, `make test`: 10706 passed, 3 skipped.
**Критик:** approve (C, sonnet), зауваження low: тест фіксує формулювання, а не поведінку.
**Заблоковано (не брались):**
• options-playbook п.4 — черга порожня (4.11); agents-mcp-access — Q-20261114-2; x-traders — гілка btc-charlie чекає власника;
• profit-search п.2 — партії 3–4 без кандидаток; structural-edges 8f — чекає до 2028; demo-setups-depth 6 — жоден сетап не має ≥30; short-setups — час; book-walls — дані з 15.12; sieve — пауза.
**Далі:** `owner`; нових питань нема.
