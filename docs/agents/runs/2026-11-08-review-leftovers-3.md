**Крок:** p, постійна картка `IDEA-20261102-standing-improvements`, новий пункт 260 (лишки BACKLOG 6.52; відкритих пунктів було менше трьох, інші картки чекають часу чи власника).

**Результат:**
• `dex.num` повертає `None` на `10**400` замість `OverflowError`.
• `perps_funding_compare`: mark / index з NaN / inf / нечислом → `null` з попередженням із назвою біржі (ccxt, Hyperliquid, Deribit).
• За рев'ю: `OverflowError` закрито й у `_finite_price`, і в `_finite_rate`.
• 21 новий тест; `make lint` чисто, `make test`: 7245 passed, 3 skipped.

**Критик (C, sonnet):** request changes — блокер `OverflowError` у `_finite_price` / `_finite_rate`; виправлено в одному раунді. Повторного рев'ю критика не було (правка в один рядок, перевірена мною за diff і тестами).

**Далі:** `owner` — на огляд. Нових питань немає.
