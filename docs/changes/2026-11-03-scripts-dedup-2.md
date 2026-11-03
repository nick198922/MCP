### 2026-11-03 — Скрипти: дублікати, друге коло (копія БД і `mode=ro`)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 33 (продовження пункту 18,
  [перше коло](2026-11-02-scripts-dedup.md)).
- `scripts/common_helpers.py`: нова `backup_db(src, dst)` — копія бази через онлайн-`backup` SQLite (узгоджена, поки
  колектор пише); джерело відкривається через `connect_ro` (лише читання, шлях екранований), тека `dst` створюється,
  наявна копія замінюється, обидва з'єднання закриваються й при помилці. Нею замінено 3 копії:
  `verify_carry_cost_calibration.snapshot` (видалено), `verify_pm_h7_iv.copy_db` і `verify_pm_modules.copy_db`
  (лишились обгортками на один рядок: `verify_pm_h7_qlike` кличе `VH.copy_db`). Відмінності старих копій зведено до
  однієї поведінки: `mkdir` тепер і в `verify_carry_cost_calibration`, закриття при помилці — і в `verify_pm_*`;
  `with b:` прибрано (після `backup` відкритої транзакції немає, коміт нічого не робив).
- `file:{db}?mode=ro` без екранування замінено на `connect_ro` (той самий `timeout`) у 9 скриптах:
  `verify_carry_cost_calibration`, `verify_pm_h7_iv` (2), `verify_pm_modules`, `verify_pm_h7_qlike`,
  `cycle_regime_eval`, `journal_rules` (2), `journal_refresh` (`timeout=10`), `traders_report` (2),
  `verify_smart_money`. Для шляхів без `?`/`#`/`%`/пробілу URI той самий; з ними тепер відкривається саме той файл
  і лише для читання (раніше `?` у шляху обрізав ім'я і міг скинути `mode=ro`).
- Змінено 10 скриптів (разом із `common_helpers.py`). Живцем 03.11: `cycle_regime_eval.py --db <база сервера>` —
  журнал 13 днів, 5 епізодів, як і раніше; усі 9 скриптів імпортуються.
- Тести: `tests/test_common_helpers.py` — `backup_db` на шляхах із пробілом і `?` (і в джерелі, і в копії; поруч
  нічого не створюється; рядок `str` теж; стара копія замінюється, а не доповнюється), відсутнє джерело → виняток і
  копії немає, при збої `backup` обидва з'єднання закриті.

**Що лишилось (не чіпали):**
- `mode=ro` без екранування: `dashboard.table_freshness`, `bybit_import.py` (2), `verify_free_supply_history.py`
  (сталий URI), а також `agents_status.server_line` і заморожені `condor_forward_costs.py` (3),
  `pm_gap_paper_report.py`, `paper_carry_report.py` — за правилами картки не чіпаємо.
- Власна екранована копія `connect_ro`: `verify_demo_execution.py` (2 місця) — можна замінити наступним кроком.
- Копії `sqlite3` backup: лише в заморожених `paper_carry_report._snapshot` і `condor_forward_costs.copy_db`.
- Копії `has_table` (запит до `sqlite_master`): `cycle_regime_eval.load`, `pm_gap_paper_report.py`.
- Без змін із першого кола: форматери `_usd` / `_pct` / `_r` / `_day` (тексти різні), `error_reply` у `journal_*_tg`
  і `rules_pretrade_tg`, `real_trades`, `_now()`, і в `verify_*.py` — `rss_mb`, `rd`, `emit`, `ymd`, `wr`.
