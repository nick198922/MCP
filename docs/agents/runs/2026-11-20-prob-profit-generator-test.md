**Крок:** p — усі пункти карток власника заблоковані (плейбук без нових правил, `mcp-access` чекає на питання, profit-search без нових карток, Polymarket H11 чекає даних, book-walls код зроблено — чекає вмикання на сервері); взято поповнення `standing-improvements`.

**Результат:**
• Тест `test_strangle_line_drops_the_probability_only_off_the_put_grid`: стренгл поза сіткою путів друкується без `(≈ N%)`, на повній сітці — з ним.
• Коментар до строгої перевірки `<= lo` у `_prob_outside` (допуск |delta| < 0.03 — лише якщо `null` частий); код не змінено.
• Записи: `docs/changes/2026-11-20-prob-profit-generator-test.md`, рядок у картці, речення в HANDOFF.

**Критик:** C (Sonnet) — approve, два зауваження без блокування (рядок картки без номера; хеш коміту не звірено).

**Перевірка:** `make lint` чисто, `make test` 10938 passed, 3 skipped.

**Далі:** `owner`. Для власника: колектор стін коду не потребує — треба `TRADEDESK_COLLECT_BOOK_WALLS_ENABLED=1` у `deploy/features.env` (Claude на сервері). `mcp-access` чекає на питання про правку `.claude/agents/*`.
