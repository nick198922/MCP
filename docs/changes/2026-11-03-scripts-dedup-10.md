### 2026-11-03 — Скрипти: дублікати, десяте коло (`rd` → `read_json`, `day_of` / `_d` → `ymd_ms`)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 41 (продовження
  [дев'ятого кола](2026-11-03-scripts-dedup-9.md)). Нових хелперів немає: беремо наявні `read_json` і `ymd_ms` зі
  `scripts/common_helpers.py`.
- У 6 скриптах прибрано 6 локальних копій, виклики тепер `cmn.…` (77 місць): `read_json` замість `rd` у
  `verify_hl_binance_spread` (21), `verify_holders_revenue` (19), `verify_taker_imbalance` (5),
  `verify_token_unlocks` (25); `ymd_ms` замість `day_of` у `verify_market_makers_depth` (2) і `_d` у
  `demo_vs_backtest` (5). У перших чотирьох додано `import common_helpers as cmn` (у `verify_taker_imbalance` — поруч
  з `vlf`, після `sys.path.insert`, з `noqa: E402`). Імпорти `json` / `datetime` / `UTC` лишились: ними користується
  решта коду.
- Ці імена ніхто ззовні не бере: чотири `verify_*` ніхто не імпортує; `verify_market_makers_depth` вантажить лише
  `tests/test_common_helpers.py` (`rd`, `wr`, `rss_mb`, `cmn`); `demo_vs_backtest` імпортують
  `demo_vs_backtest_weekly_tg.py` і тести, `_d` серед них не бере ніхто (Grep `(script|dv)\._d`).
- Вивід той самий. Заміни робив скрипт за AST, як у дев'ятому колі: тіло має збігатися з еталоном
  (`json.loads(p.read_text())` або `datetime.fromtimestamp(ms / 1000, UTC | tz=UTC).strftime("%Y-%m-%d")`), ім'я —
  траплятися лише як читання, і кількість збігів регулярного виразу дорівнює кількості вузлів AST (у рядках і
  коментарях імені немає). До/після: старі тіла з `HEAD` проти спільних функцій — JSON на 8 файлах (не-ASCII, `null`,
  float, великі цілі, компактний і звичайний) і `ymd_ms` на 1596 мітках (−1 день … 2100 рік, межі доби ±1 мс навколо
  2024 з 29.02) — усі збіглись. Два задовгі рядки у `verify_hl_binance_spread` переніс `ruff format` (текст той
  самий). Усі 6 скриптів вантажаться, локальних імен у них немає, `mod.cmn` — той самий модуль; тести
  `test_demo_vs_backtest`, `test_demo_shadow`, `test_dashboard_demo`, `test_demo_vs_backtest_weekly_tg`,
  `test_common_helpers` — 147 пройшли. Тест не додавали: нового хелпера немає.

**Що лишилось для BACKLOG 6.4 (не чіпали):**
- `ymd_ms`: `day_str` у `sieve.py` — тіло однакове, `sv.day_str` ніхто не бере (`sieve` імпортують
  `verify_dca_martingale_sieve`, `verify_grid_short_vol_regime`, `tests/test_strategy_sieve.py`), кандидат на наступне
  коло. Лишити ім'я (`x = cmn.…`): `verify_liquidation_flush` (`vlf.day_str`, `vlf.read_json`),
  `verify_news_events`, `verify_option_modules`, `verify_setup_passport`, `verify_course_s14_ema` (`s1.day_of`,
  `s1.rd` / `s1.wr`), `research/vault_profile`.
- `ymd_day`: лишити ім'я у `verify_funding_crowding`, `verify_listing_drift`.
- Без змін: 46 локальних `rss_mb` (3 варіанти, 7 модулів реекспортують), однакові `_r` у `calls_hourly.py` /
  `demo_vs_backtest.py`; не зливати варіанти `wr` (у цих чотирьох — без компактних роздільників), `ymd(… | None)` з
  `"—"`, `iso` з `%H:%M`, `_usd` / `_pct`.
