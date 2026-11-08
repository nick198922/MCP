**Крок:** p, постійна картка standing-improvements (інші картки власника заблоковані часом/власником); додано пункт 72 з BACKLOG 6.9.
**Результат:**
• Тести для 5 функцій `analytics/`: `gex_by_strike`, `atm_straddle`, `oi_by_strike`, `detect_interval_hours`, `g_shifted_prob` — 37 тестів на аналітичних еталонах, `src/` не змінено.
• Знайдено 2 дрібні розбіжності (docstring `detect_interval_hours`; notional у `oi_by_strike` за кількох експірацій) — BACKLOG 6.12, не виправлено.
• `make lint` і `make test` пройшли (4539 passed).
**Критик:** C (sonnet) — approve; зауваження не блокують.
**Далі:** `owner`. Нових питань немає.
