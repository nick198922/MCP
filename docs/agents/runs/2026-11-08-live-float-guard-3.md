**Крок:** p/постійна картка, пункт 265 (додано координатором): захист від зіпсованих полів живих даних, що лишились у BACKLOG 6.53.

**Результат:**
• Захищено 5 місць: `demo_vs_backtest.day_turnover`, `pm_calibration` (`resolved_outcome`, `_yes_prints`), `pm_dislocation.yes_prints`, `pm_gap_report.binance_closes`, `sentiment._norm`.
• `OverflowError` та NaN/inf тепер відкидаються; `sentiment_fear_greed` дає попередження.
• 83 нові тести (`tests/test_live_float_guard_3.py`); `make lint` і `make test` (7403 passed, 3 skipped) — мої.
• Лишилось у BACKLOG 6.53: `smart_money`, `pm_calibration.lag_profile` / `jump_proximity`, два місця в `collector/`.

**Критик:** approve (режим C), блокерів нема; [рев'ю](../../reviews/2026-11-08-live-float-guard-3.md). Живий запуск 08.11 з цього середовища не перевірити.

**Далі:** `owner` — крок завершено, нових питань немає.
