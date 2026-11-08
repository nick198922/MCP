**Крок:** p/standing-improvements (пункт 81): усі відкриті пункти карток власника заблоковані (дані, час, власник); додано й виконано тест `stage0_condor_forward.py`.

**Результат:**
• 2 тести в `tests/test_forward_scripts.py` на синтетичному кеші `put_skew`: звіт має розділи, SD/MDE звірено, заборонених полів (`hold_*`, P&L, кредит) немає.
• Знайдено дефект: порожній кеш дає traceback — BACKLOG 6.14, код скрипта не змінювано.
• `make lint && make test`: 4585 passed, 2 skipped.

**Критик:** C — approve ([рев'ю](../../reviews/2026-11-05-stage0-condor-test.md)); коментар виправлено (6.14).

**Далі:** `owner`. Питань немає.
