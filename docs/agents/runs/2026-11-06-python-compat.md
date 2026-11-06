**Крок:** p, постійна картка `standing-improvements`: відкритим лишався лише 8a (чекає сервера), тож додано пункт 158 (сумісність з Python ≥3.11).

**Результат:**
• Новий `tests/test_python_compat.py`: 626 файлів (`src/` + `scripts/`), синтаксис 3.12+ та імена stdlib з 3.12/3.13.
• Порушень 0, `src/` і `scripts/` не змінено, allowlist порожній.
• `make lint` і `make test` чисті: 6014 passed, 3 skipped.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-06-python-compat.md). Не покрито `tests/`, Python 3.14 і методи екземплярів із загальними іменами (BACKLOG 6.40).

**Далі:** `owner`. Нових питань немає.
