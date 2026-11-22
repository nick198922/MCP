**Крок:** p, standing-improvements: усі три відкриті пункти заблоковані, тож додано пункт 420 із рев'ю «не блокує» (s38-cutoff-counters) і виконано. Інші картки Порядку 0 заблоковані: options-playbook п. 4 (черга порожня), agents-mcp-access (Q-20261114-2), x-traders (відкрита гілка), profit-search (нових кандидаток нема), structural-edges 8f і short-setups (чекають часу), book-walls (дані за 15.12).

**Результат:**
• Тіньові плани в `analytics/demo_shadow.py`: план, закритий після `now_ms`, рахується відкритим; `verify_s38` і `verify_s12` передають cutoff, тож повтор на cutoff дає той самий звіт.
• `at_cutoff`: closed без `closed_ts` знову не «open» + тест.
• `verify_s3` не змінено: годинника не читає. Обмеження: у missed нема часу зміни.
• Lint чистий, `make test`: 10914 passed, 3 skipped. На живій БД не прогонялось.

**Критик (C):** approve, три зауваження не блокують (s12 без тесту, заголовок розділу, missed).

**Далі:** owner. Питань нових нема.
