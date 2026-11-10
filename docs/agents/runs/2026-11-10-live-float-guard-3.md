**Крок:** p — стояча картка `IDEA-20261102-standing-improvements`; відкриті пункти чекали даних сервера, тож додано пункт 308 (залишок BACKLOG 6.53).

**Результат:**
• Шість розборів (`demo_vs_backtest`, `pm_calibration`, `pm_dislocation`, `pm_gap_report`, `sentiment`, `smart_money`) уже ловили `OverflowError`, але нові «отрути» (1e308, 4300+ цифр, рядки не тієї форми) знаходили дефекти в кожному — виправлено.
• Межі: оборот ≤ 1e20 USD, суми ±1e15 USD, час до 9999 року; ціни Polymarket поза 0..1 пропускаються.
• 248 нових тестів `tests/test_tool_failure_extra_15.py`; живі перевірки (Fear & Greed, 1617 ринків Gamma, HL-зразок) без розбіжностей.
• `make lint` і `make test` у мене: 9168 passed, 3 skipped.

**Критик:** C (sonnet) — approve, блокерів нема. Зауваження: константа `MAX_TS_S` продубльована у трьох файлах; `true`/`false` у числових полях читається як 1/0.

**Далі:** owner. Відкрите: `pm_threshold._json_list` і `polymarket._maybe_json_list` мають той самий дефект 4300+ цифр (з читання коду); у BACKLOG рядок 183 — старий дубль 6.53.
