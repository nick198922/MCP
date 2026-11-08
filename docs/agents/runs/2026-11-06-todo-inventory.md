**Крок:** p — постійна картка вдосконалень, пункт 147 (інвентар TODO/FIXME/XXX).

**Результат:**
• `scripts/todo_inventory.py` (stdlib, лише читання, `--out`, пошук через `tokenize.COMMENT`) + `tests/test_todo_inventory.py` (4 тести).
• Прогін по репозиторію: 621 файл, TODO/FIXME/XXX у коментарях — 0, не згаданих у BACKLOG — 0.
• BACKLOG 6.32, зміни `docs/changes/2026-11-06-todo-inventory.md`, пункт 147 відмічено.
• `make lint && make test`: 5220 passed, 3 skipped.

**Критик:** C (sonnet) — approve, блокерів немає ([рев'ю](../../reviews/2026-11-06-todo-inventory.md)).

**Далі:** `owner`. Відкритим лишається лише 8a (чекає сервера); коли відкритих пунктів менше трьох, наступний поповнюється з «Як поповнювати».
