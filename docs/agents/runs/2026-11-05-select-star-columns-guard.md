**Крок:** p, постійна картка `IDEA-20261102-standing-improvements` (відкритих пунктів лишалось 1 — 8a, чекає сервера; додано пункт 97 з BACKLOG 6.15). Решта карток власника заблоковані (дата / власник).

**Результат:**
• `tests/test_report_sql_schema.py`: охоронець колонок для чотирьох `SELECT *` (`demo_trade`, `cp_position`, `pm_gap_outcome`, `option_leg_snapshot`) — ключі, які читають звіти й `analytics/`, звіряються зі схемою 38.
• Розбіжностей немає (10 / 8 / 8 / 20 ключів); `src/`, колектори й `execution/` не змінено.
• Обмеження: місця читання рядків перелічено вручну; JSON-колонки `cp_position` — SKIP.
• `make lint` і `make test` (4644 passed, 3 skipped) — мої прогони.

**Критик:** C — approve ([рев'ю](../reviews/2026-11-05-select-star-columns-guard.md)), блокерів немає.

**Далі:** owner. Питань немає.
