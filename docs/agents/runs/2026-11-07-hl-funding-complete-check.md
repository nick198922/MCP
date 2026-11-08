**Крок:** p/стала картка `IDEA-20261102-standing-improvements`: відкритих пунктів лишався один (8a, чекає даних сервера), тому додано пункт 195 з BACKLOG 6.41.
**Результат:**
• Перед роботою звірено код: `funding_history_range` (Hyperliquid) уже ставить `complete=false` і рахує `dropped_rows` при зіпсованому рядку.
• `tests/test_pagination.py` — 11 passed; код не змінено.
• Застарілий рядок у BACKLOG 6.41 закреслено, пункт 195 закрито без коду ([зміни](../../changes/2026-11-07-hl-funding-complete-check.md)).
• `make lint` чисто; `make test` — 6754 passed, 3 skipped.
**Критик:** не залучався (лише документи, коду не змінено).
**Далі:** `owner`. У картці знову один відкритий пункт (8a) — наступний прогін додасть ще з «Як поповнювати». Гілка `agent/2026-11-04-library-seller-exhaustion-card` досі чекає власника.
