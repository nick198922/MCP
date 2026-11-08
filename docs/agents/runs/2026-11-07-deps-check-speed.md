**Крок:** постійна картка `standing-improvements` (крок p): усі інші картки власника чекають часу, даних сервера чи Claude; відкритих пунктів було менше трьох, тож додано пункт 192 (BACKLOG 6.50).

**Результат:**
• `scripts/deps_check.py::imports_of` обходить лише інструкції (поля `body/orelse/finalbody/handlers/cases`), а не вирази.
• Вивід `deps_check` на репозиторії побайтно той самий; таблиця з 8479 імпортів у 950 файлах теж збіглась.
• `test_main_on_repository` 10.0 → 5.9 с, `..._unconditionally` 6.6 → 3.9 с, весь файл 16.8 → 11.4 с.
• Новий тест на вкладені місця імпорту (функція, клас, try/except*, match, with тощо).
• Чек: `make lint` ✓, `make test` 6753 passed, 3 skipped.

**Критик:** C (sonnet) — approve ([рев'ю](../../reviews/2026-11-07-deps-check-speed.md)).

**Далі:** `owner`. У BACKLOG 6.50 лишається `deribit_vrp` (код `analytics/vrp.py`). Нових питань немає; відкрите питання Q-20261106-1 (поріг для правила 48 год) чекає на відповідь.
