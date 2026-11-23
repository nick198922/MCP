**Крок:** p → усі картки власника заблоковані чи порожні; поповнено постійну картку (пункт 451) і взято його.
• Заблоковано: options-playbook п. 4 (черга порожня, 4.10–4.12), agents-mcp-access (Q-20261114-2: сесія не має запису в `.claude/`), x-traders (гілка `x-btc-charlie-sample-registration` змінює картку), profit-search п. 2 (нових ◯ нема), structural-edges-depth 8f/8k/8l (час), demo-setups-depth п. 6 (S39/S14 уже звірено 22.11), book-walls (вмикач на сервері; п. 2 з 15.12), sieve (пауза).

**Результат:** пункт 451 (R1/R4 у `classify` до виходу «no data») **уже зроблено** пунктом 447 (`4c872aad`); код не змінено.
• Тест є: `test_no_data_still_shows_bar_free_flags`.
• Додано `docs/changes/2026-11-23-carry-classify-r1-r4-before-nodata.md`, пункт 451 закрито в картці.
• `make lint` чисто; `make test`: 11030 passed, 3 skipped.

**Критик:** не запускався (лише docs, коду нема).

**Далі:** `owner`. Питання: agents-mcp-access досі чекає, що власник сам допише `mcp__tradedesk` у `.claude/agents/*.md` (Q-20261114-2). Постійній картці знову бракує незаблокованих пунктів.
