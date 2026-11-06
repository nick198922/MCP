**Крок:** p, постійна картка standing-improvements; відкритим лишався лише 8a (чекає сервера), тож додано пункт 181 з BACKLOG 6.48.
**Результат:**
• `tests/test_tools_envelope_shape.py`: усі 109 інструментів тепер дають `ok=true` на фейках (було 100; `ok=false` 9 → 0).
• Нові покриття: Coinalyze ×4, `polymarket_options_gap`, `event_study`, `positions_risk`, `carry_paper_close`, `range_setup`.
• Порушень форми не знайдено; `src/`, `execution/`, колектори не чіпали.
• `make lint` і `make test` пройшли (6591 passed, 3 skipped).
**Критик:** C (sonnet) — approve, два зауваження «не блокує» ([рев'ю](../../reviews/2026-11-06-envelope-shape-3.md)).
**Далі:** `owner`; нових питань немає. BACKLOG 6.48 закрито.
