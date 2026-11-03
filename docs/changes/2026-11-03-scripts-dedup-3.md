### 2026-11-03 — Скрипти: дублікати, третє коло (`mode=ro`, `connect_ro`, `has_table`)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 34 (продовження пункту 33,
  [друге коло](2026-11-03-scripts-dedup-2.md)).
- `file:{db}?mode=ro` без екранування замінено на `connect_ro` з `scripts/common_helpers.py`:
  `dashboard.table_freshness`, `bybit_import.py` (2: `stats` і `import-classic --symbols journal`; локальні
  `import sqlite3` прибрано), `verify_free_supply_history.our_snapshots` (сталий URI `DB` став звичайним шляхом).
  `timeout` той самий (5 с, як і в `sqlite3.connect` за замовчуванням); `open_ro` не взято, щоб не міняти поведінку
  (`table_freshness` і далі сама перевіряє `exists()`).
- `verify_demo_execution.py`: власна екранована копія `connect_ro` (2 місця, `load` і `load_signals`) замінена на
  спільну; `urllib.parse` більше не потрібен. `cycle_regime_eval.load`: запит до `sqlite_master` замінено на
  `cmn.has_table` (той самий запит, `type = 'table'`).
- Живцем 03.11 на базі сервера: `cycle_regime_eval.py` — журнал 13 днів, 5 епізодів, як і раніше;
  `table_freshness` — 9 таблиць; `our_snapshots` і `bybit_import.stats` читають; усі скрипти, що імпортують
  `bybit_import` (`hyperliquid_import`, `binance_import`, `bybit_options_positions`, `bybit_live`), імпортуються.
- Тести: `tests/test_common_helpers_callers.py` — кожен змінений випадок на шляху з пробілом, `?` і `#`: читає саме
  той файл, поруч нічого не створюється (для журналу `Store` у WAL — лише `-wal`/`-shm`); `has_table` не бере view.
- Помічено, не чіпали: `bybit_import.stats` падає `ZeroDivisionError`, коли збиткових угод нема (`sum(losses)` у
  рядку «10 найбільших збитків»).

**Що лишилось для BACKLOG 6.4 (не чіпали):**
- `mode=ro` без екранування: `agents_status.server_line` і заморожені `condor_forward_costs.py` (3),
  `pm_gap_paper_report.py`, `paper_carry_report.py` — за правилами картки не чіпаємо.
- Копії `sqlite3` backup — лише в заморожених `paper_carry_report._snapshot` і `condor_forward_costs.copy_db`;
  копія `has_table` — у замороженому `pm_gap_paper_report.py`.
- `dashboard.py`: три місця з `SELECT name FROM sqlite_master WHERE type = 'table'` (множина назв, а не одна
  таблиця — не `has_table`).
- Без змін із першого кола: форматери `_usd` / `_pct` / `_r` / `_day` (тексти різні), `error_reply` у `journal_*_tg`
  і `rules_pretrade_tg`, `real_trades`, `_now()`, і в `verify_*.py` — `rss_mb`, `rd`, `emit`, `ymd`, `wr`.
