**Крок:** p — постійна картка `standing-improvements` (Порядок 1): відкритим лишався тільки 8a (чекає сервера), тож додано пункт 59 з BACKLOG 6.8.

**Результат:**
• Формулу «Δ_reg» у `IDEA-20261016-course-setups-crypto.md` взято в backticks (екранування дужок не допомогло б: тест шукає `](`).
• `ALLOWLIST` у `tests/test_docs_links.py` тепер порожній; BACKLOG 6.8 закреслено, пункт 59 відмічено.
• `make lint` чисто, `make test`: 4428 passed, 2 skipped.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-04-docs-links-allowlist-cleanup.md).

**Далі:** `owner`. Інші картки з пріоритетом або чекають власника / часу, або не мають відкритих пунктів для агентів; нових питань немає.
