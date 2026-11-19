**Крок:** постійна картка `standing-improvements`, новий пункт 396 (джерело — BACKLOG 6.53): усі картки з Порядком 0–1 заблоковані (питання власнику, час, дані сервера), черга плейбука порожня.

**Результат:**
• 32 нові тести «збій / порожнє сховище» для 8 інструментів (`skew_percentile`, `funding_zscore`, `liquidations_summary`, `event_study`, `ohlcv_history`, `positions_risk`, `rules_test`, `touch_probability`) — `tests/test_tool_failure_extra_26.py`.
• Знайдено хибу: `event_study` без жодної події з повним вікном барів відповідав `ok=true` з порожніми статистиками; тепер `ok=false` + `missing: ohlcv_hist`.
• Побачено, не виправлено (у BACKLOG 6.53): `liquidations_summary` не відрізняє тихе вікно від вимкненого колектора; `positions_risk` без `mark` і `reference_spot` рахує нульовий рух.
• `make lint` і `make test` — 10600 passed.

**Критик:** C (sonnet) — approve, блокерів нема; зауваження: один тест не звіряє вміст `data`, нема юніт-тесту на нову гілку `event_study`.

**Далі:** `owner`. Нових питань нема.
