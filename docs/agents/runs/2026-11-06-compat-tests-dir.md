**Крок:** p, постійна картка `IDEA-20261102-standing-improvements`: відкритих пунктів лишилось менше трьох, додано пункт 178 (BACKLOG 6.40).

**Результат:**
• `tests/test_python_compat.py` тепер сканує і `tests/*.py` (без `tests/fixtures/`): +321 файл, разом 947
• Порушень 0, виправлено 0, `ALLOWED` порожній; тест-файл 705 → 1026 тестів
• `make lint` чисто, `make test`: 6577 passed, 3 skipped

**Критик:** approve (sonnet, [рев'ю](../../reviews/2026-11-06-compat-tests-dir.md)); не блокує: виключення `tests/fixtures/` не перевірене на реальних даних, бо там немає `.py`.

**Далі:** `owner`. Решта відкритих пунктів картки — 8a (чекає даних сервера). Інших гілок агентів, що чекають, немає, крім `agent/2026-11-04-library-seller-exhaustion-card`.
