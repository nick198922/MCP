### 2026-11-03 — Скрипти: дублікати, дев'яте коло (`ymd` / `day_str` → `ymd_day`, `rd` → `read_json`)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 40 (продовження
  [восьмого кола](2026-11-03-scripts-dedup-8.md)). Нових хелперів немає: беремо наявні `ymd_day` і `read_json` зі
  `scripts/common_helpers.py`.
- У 10 скриптах прибрано 10 локальних копій, виклики тепер `cmn.…` (104 місця): `ymd_day` замість `ymd(day)` у
  `verify_oi_turnover_short`, `verify_residual_momentum_short_leg`, `verify_sector_pair_spread`,
  `verify_toptrader_divergence`, `verify_weekend_idio_reversal`, `verify_xs_anchor_nearness`,
  `verify_xs_volume_liquidity_factors` і замість `day_str` у `verify_course_s1_spot_alts` (82 виклики); `read_json`
  замість `rd` у `verify_free_supply_history` і власного `read_json` у `verify_tradfi_offhours_reopen` (22 виклики).
  Імпорти `json` / `datetime` / `UTC` лишились: ними користується решта коду цих файлів.
- Ці імена ніхто ззовні не бере: `verify_sector_pair_spread` вантажить `tests/test_pair_spread.py` (лише
  `log_returns`, `admitted`, `FORM_DAYS`), `verify_free_supply_history` — два тести (`cmn`, `our_snapshots`).
- Вивід той самий. Заміни робив скрипт за AST, як у восьмому колі: тіло має збігатися з еталоном
  (`datetime.fromtimestamp(day * 86400, UTC | tz=UTC).strftime("%Y-%m-%d")` або `json.loads(p.read_text())`, `UTC` і
  `datetime` — з модуля `datetime`), ім'я — траплятися лише як читання (не параметр, не присвоєння, не імпорт). Рядки,
  що після заміни стали задовгими, переніс `ruff format` (4 місця у 3 файлах, текст той самий). До/після: старі тіла з `HEAD`
  проти спільних функцій на 116 зразках (номери днів −1…29 999 з високосним 29.02; JSON з не-ASCII, `null`, float,
  компактний і звичайний) — усі збіглись. Усі 10 скриптів вантажаться, локальних імен у них більше немає, `mod.cmn` —
  той самий модуль. Тест не додавали: нового хелпера немає.

**Що лишилось для BACKLOG 6.4 (не чіпали):**
- `read_json`: `rd` у `verify_hl_binance_spread`, `verify_holders_revenue`, `verify_taker_imbalance`,
  `verify_token_unlocks` (тіла однакові з еталоном, перевірено тим самим AST-скриптом; не влізли в ліміт 10 файлів).
  Їхні `ymd(… | None)` з `"—"` і `wr` без компактних роздільників — інші, не зливати.
- `ymd_ms` під іншими іменами: `verify_market_makers_depth` (`day_of`), `demo_vs_backtest` (`_d`), `sieve` (`day_str`;
  перевірити, хто імпортує). Лишити ім'я (`x = cmn.…`): `verify_liquidation_flush` (`vlf.day_str` і `vlf.read_json`
  беруть `verify_oi_liquidation_flush`, `verify_btc_shock_alt_lag`), `verify_news_events`, `verify_option_modules`,
  `verify_setup_passport`, `verify_course_s14_ema` (`s1.day_of`, `s1.rd` / `s1.wr`), `research/vault_profile`.
- `ymd_day`: лишити ім'я у `verify_funding_crowding`, `verify_listing_drift`.
- Без змін: 46 локальних `rss_mb` (3 варіанти, 7 модулів реекспортують), однакові `_r` у `calls_hourly.py` /
  `demo_vs_backtest.py`; не зливати варіанти `wr`, `ymd(ms | None)` з `"—"`, `iso` з `%H:%M`, `_usd` / `_pct`.
