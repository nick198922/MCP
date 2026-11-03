### 2026-11-03 — Скрипти: дублікати, п'яте коло (`rss_mb`)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 36 (продовження
  [четвертого кола](2026-11-03-scripts-dedup-4.md)).
- Форматери `_usd` / `_pct` не чіпали: усі копії різні (знак, `—` / `n/a` / `-`, множення на 100, кількість знаків,
  мінус `−`), спільної версії без зміни тексту звітів нема. Однакова пара лише `_r` у `calls_hourly.py` і
  `demo_vs_backtest.py` (`"—"` або `f"{x:+.2f}"`) — лишили на наступне коло через ліміт 10 файлів.
- Найбільша однакова копія — `rss_mb()` (пікова RSS у МБ, 70+ копій у `verify_*.py`). Винесено в
  `scripts/common_helpers.py`; у 10 скриптах локальну копію й `import resource` прибрано, виклики — `cmn.rss_mb()`:
  `verify_carry_cost_calibration`, `verify_free_supply_history`, `verify_negrisk_step0`, `verify_paper_carry_bench`,
  `verify_pm_h11_recheck`, `verify_pm_h7_iv`, `verify_pm_modules`, `verify_pm_touch`, `verify_smart_money`,
  `verify_unlock_schedules`. Вивід той самий: `ru_maxrss / 1024` і `/ 1024.0` дають одне й те саме число.
  Обрано скрипти, з яких `rss_mb` ніхто не імпортує (`vsp.`, `v0.`, `dl.`, `dsm.`, `s7.`, `base.` — не ці).
- Тести: `tests/test_common_helpers.py` — `rss_mb` на підміненому `getrusage` (те саме значення, що давала копія
  з `/ 1024.0`), на живому процесі, і 4 змінені скрипти без власної копії. Усі 10 скриптів (і
  `verify_pm_h7_qlike`, що вантажить `verify_pm_h7_iv`) імпортуються.

**Що лишилось для BACKLOG 6.4 (не чіпали):**
- `rss_mb`: ще ~60 копій у `verify_*.py`; 7 із них інші скрипти беруть як атрибут модуля (`verify_setup_passport`,
  `verify_news_events`, `verify_carry_a_delisting`, `verify_demo_stop_management`, `verify_session_stop_hunt`,
  `verify_market_makers_onchain`, `verify_course_step7_basics`) — там лишити ім'я (`rss_mb = cmn.rss_mb`).
  Варіанти-рядки `rss()` (`peak RSS … MB` тощо) — тексти різні.
- `_r` у `calls_hourly.py` / `demo_vs_backtest.py` (однакові); варіанти `_r` у `demo_vs_backtest_weekly_tg.py`
  (мінус `−`), `dashboard.py` (` R`), `verify_negrisk_step0.py` / `verify_s38_modules.py` (інший сенс).
- Однакові пари в `verify_*.py`: `rd` / `wr` у `verify_news_events.py` і `verify_alt_downside_beta.py`;
  `ymd(day)` у `verify_alt_downside_beta.py` і `verify_xs_factor_ic_panel.py`; `ymd(ms)` у `verify_news_events.py`,
  `verify_weekly_straddle.py`, `verify_option_modules.py` (і `iso` / `_day` з тим самим тілом). `emit` залежить від
  модульного `OUT` — не спільний.
- Без змін: форматери `_usd` / `_pct` / `_day` (тексти різні), `error_reply`, `real_trades`, `_now()`; заморожені
  форварди, `agents_*` не чіпаємо.
