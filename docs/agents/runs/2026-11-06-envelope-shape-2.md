**Крок:** p — постійна картка (`Порядок: 1`), пункт 176 (додано з BACKLOG 6.48); картка глибини заблокована (≥30 угод / рішення власника).
**Результат:**
• `tests/test_tools_envelope_shape.py`: перевірено ще 15 інструментів (`ok=true` 85 → 100, `ok=false` 24 → 9).
• Порушень форми Envelope 0, `src/` не змінено; фейки зі збережених зразків.
• Лишилось 9 (Coinalyze ×4, `polymarket_options_gap`, `event_study`, `positions_risk`, `carry_paper_close`, `range_setup`) — BACKLOG 6.48.
• `make lint` чисто, `make test`: 6238 passed, 3 skipped.
**Критик:** C (sonnet) — approve, зауваження не блокують ([рев'ю](../../reviews/2026-11-06-envelope-shape-2.md)).
**Далі:** `owner`. Незмерджена гілка `agent/2026-11-04-library-seller-exhaustion-card` чекає власника.
