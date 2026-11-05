**Крок:** p, постійна картка `standing-improvements` (усі пункти закриті, окрім 8a, що чекає сервера); додано пункт 110 з BACKLOG 6.18.

**Результат:**
• `plan_add` з `expires_ts` у мілісекундах більше не кидає виняток: `_iso` показує сире число, відповідь — `invalid_input` (інваріант 1).
• 2 нові тести в `tests/test_trade_plan.py`; інших вразливих `_iso` у `tools/` немає.
• `make lint` і `make test`: 4760 passed, 3 skipped.

**Критик:** C (sonnet): approve, [рев'ю](../../reviews/2026-11-05-plan-add-ms-expires.md).

**Далі:** `owner`. Можлива дрібна знахідка: `event_study` на абсурдних `ts_ms` з `event_add` — не перевірено (кандидат на наступний пункт).
Гілка `agent/*-fix-*` не потрібна; одна гілка `agent/2026-11-04-library-seller-exhaustion-card` чекає власника.
