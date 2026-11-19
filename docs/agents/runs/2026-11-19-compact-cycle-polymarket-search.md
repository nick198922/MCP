**Крок:** постійна картка `standing-improvements`, пункт 405 (доданий координатором із BACKLOG «великі відповіді за замовчуванням»). Усі картки власника з відкритими пунктами зараз заблоковані (дані, час, питання, гілка x-traders чекає власника).

**Результат:**
• `cycle_regime`: типова відповідь 12 443 → 6 705 символів (історія — 10 останніх днів + `history_summary`); повний вивід — `include_details=true`.
• `polymarket_search`: ≈ 12 200 → 3 925 (10 ринків, без slug / token_id); повний — `include_details=true`.
• Споживач `assistant_snapshot` переведено на `include_details=true`; нові тести розміру, повного режиму й збою провайдера.
• `make lint` чисто, `make test`: 10750 passed, 3 skipped.

**Критик:** C (sonnet) — approve, без блокерів. Зауваження: у рядку `cycle_regime` у README немає згадки про параметр (дрібниця).

**Далі:** `owner` — PR на злиття. Наступні кандидати цього ряду: `market_tickers`, `system_health`. Питань нових немає; відкриті питання власника — у `docs/QUESTIONS.md` (9).
