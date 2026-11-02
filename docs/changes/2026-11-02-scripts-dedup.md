### 2026-11-02 — Скрипти: спільні хелпери читання БД

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 18.
- Новий `scripts/common_helpers.py` (лише stdlib: панель іде на системному `python3 -I`):
  - `connect_ro(path, timeout=5)` — `sqlite3.connect("file:<екранований шлях>?mode=ro", uri=True)`; рядок був
    скопійований в 11 місцях, що їх замінено (ще 2 — у `verify_demo_execution.py`);
  - `open_ro(path)` — `(з'єднання, None)` або `(None, "missing" | "unreadable: <виняток>")`, винятків не кидає; цей
    5-рядковий пролог був у 7 завантажувачах;
  - `has_table(con, name)` — запит до `sqlite_master` (замінено 4 копії; ще 2 — у `pm_gap_paper_report.py` і
    `cycle_regime_eval.py`, див. нижче).
- Замінено копії: `dashboard.py` (`load_journal`, `load_pm_gap`, `load_demo`), `rules_weekly_tg.py`,
  `forecast_weekly_tg.py`, `journal_weekly_tg.py`, `demo_vs_backtest_weekly_tg.py`, `demo_vs_backtest.py` (2),
  `collector_health.connect_ro`, `checkpoint_report.connect_ro` (обгортки з перевіркою файла лишились). URI, timeout,
  тексти причин і винятки ті самі. Користь: одне перевірене місце для «лише читання й екранований шлях».
- Живцем 02.11 на базі сервера: журнал 480 рядків, прогнози 96, демо 83 угоди / 1141 записи аудиту, знімок
  Polymarket є, здоров'я колекторів — 23 серії; помилок немає. `python3 -I scripts/dashboard.py --help` запускається.
- Тест: `tests/test_common_helpers.py` (по тесту на кожен хелпер: читання, відмова в записі, шлях із `?`/`#`/`%`,
  файл не створюється, причини `open_ro`, `has_table` не плутає view з таблицею).

**Що лишилось (не чіпали):**
- Відкриття `mode=ro` без екранування шляху (`file:{db}?mode=ro`): `dashboard.table_freshness`,
  `agents_status.server_line`, `journal_rules.py`, `journal_refresh.py`, `cycle_regime_eval.py`,
  `pm_gap_paper_report.py`, `bybit_import.py`, `traders_report.py`, `condor_forward_costs.py`, частина `verify_*.py`.
  Шляхи там фіксовані, тож шкоди зараз немає; перехід на `connect_ro` змінить поведінку для шляхів із `?`/`#`/`%` —
  окремим кроком.
- Копія БД через `sqlite3` backup: `paper_carry_report._snapshot`, `condor_forward_costs.copy_db`,
  `verify_pm_h7_iv` / `verify_pm_modules.copy_db` (різняться `mkdir` і закриттям при помилці).
- Форматери `_usd`, `_pct`, `_r`, `_day` у `dashboard.py`, `assistant_brief_tg.py`, `paper_carry_report.py`,
  `position_remind.py`, `calls_check.py`: тексти різні («—» проти «n/a», «−» проти «-»), злиття змінило б повідомлення.
- Дрібне: `error_reply` у `journal_trade_tg` / `journal_close_tg` / `journal_cancel_tg` / `rules_pretrade_tg`
  (різниться лише дієслово), `real_trades` у `rules_weekly_tg` і `journal_trade_tg`, `_now()` у `bybit_live` і
  `bybit_options_positions`.
- `verify_*.py`: `rss_mb` (58 однакових копій), `rd`, `emit`, `ymd`, `wr` — дослідницькі скрипти, прив'язані до своїх
  звітів; не чіпали.
- Відправка в Telegram і читання `.env` уже в одному місці (`tg_send.py`), дублікатів не знайдено.
