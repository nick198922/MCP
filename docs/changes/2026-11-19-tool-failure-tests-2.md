# 2026-11-19 — тести «сховище вимкнене / порожнє, біржі лежать» для 8 інструментів (пункт 396)

Інваріанти 1 і 5 `CLAUDE.md`, BACKLOG 6.53. Інструменти: `skew_percentile`, `funding_zscore`, `liquidations_summary`, `event_study`, `ohlcv_history`, `positions_risk`, `rules_test`, `touch_probability`. Збій SELECT, зіпсовані рядки й межі float у них уже мали окремі тести (пункти 279, 280, 285, 289); загальні охоронці (`test_tools_no_store`) перевіряли лише, що конверт повертається.

- Новий `tests/test_tool_failure_extra_26.py` (32 тести) фіксує, що саме каже конверт кожного інструмента:
  - сховище вимкнене (`db_path=""`) → `ok=false`, `missing: store` (config), жодного запиту до біржі;
  - сховище не відкривається → `ok=false`, `missing: store` (backend_error);
  - порожнє сховище → `ok=false`, `data_unavailable` своєї таблиці (`liquidations_summary` лишається `ok=true` «0 ліквідацій», як задокументовано; `rules_test` читає метрику без рядків як `null`, а порівняння з нею — `missing: eval`);
  - усі біржі лежать (HTTP 500, ccxt падає), дані в сховищі є → `ok=true` без жодного запиту до провайдера (`touch_probability` — ще й зі сховищем, що не відкривається);
  - `event_study`: збій запиту подій → `missing: store`.
- **Винятку назовні не знайдено.** Виправлено `event_study`: коли жодна подія не мала повного вікна барів, відповідав `ok=true` з усіма статистиками `null` і порожнім `missing`; тепер `ok=false` з `missing: ohlcv_hist` (список подій лишається в `data`), опис інструмента про це каже. Якщо оцінено хоча б одну подію — як і раніше `ok=true`.
- Відкрите (не чіпали): `liquidations_summary` не відрізняє «тихе вікно» від «колектор вимкнено»; `positions_risk` без `mark` і `reference_spot` рахує спот-сценарії з нульовим рухом без попередження.
- `execution/`, `core/`, `providers/` не чіпали.
