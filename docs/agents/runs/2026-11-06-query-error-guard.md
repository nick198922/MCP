**Крок:** p — картка `IDEA-20261102-standing-improvements`, новий пункт 166 (BACKLOG 6.43, продовження 165); решта карток із `Порядок: 0` без відкритих розблокованих пунктів.

**Результат:**
• 10 інструментів сховища (9 з пункту + `range_setup`) більше не кидають виняток при помилці sqlite під час запиту: `missing: store`, `ok=false` без даних.
• `system_health` при помилці сховища лишається `ok=true` зі `store.status="error"`.
• Тест `tests/test_tools_no_store.py` тепер вимагає `QUERY_GUARDED == STORE_TOOLS` (38), `KNOWN_RAISERS` порожній.
• `make lint` чисто, `make test`: 6192 passed, 3 skipped.

**Критик:** C (sonnet) — approve, блокерів нема; мутація guard-а валить тест. Зауваження: `STORE_ERRORS` не покриває `RuntimeError`/`ValueError` закритого з'єднання. [Рев'ю](../../reviews/2026-11-06-query-error-guard.md).

**Далі:** owner. Нових питань немає.
