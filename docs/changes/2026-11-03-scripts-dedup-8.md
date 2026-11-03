### 2026-11-03 — Скрипти: дублікати, восьме коло (`rd` / `wr` / `ymd` під різними іменами)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 39 (продовження
  [сьомого кола](2026-11-03-scripts-dedup-7.md)). Нових хелперів немає: беремо наявні `read_json`, `write_json`,
  `ymd_day`, `ymd_ms` зі `scripts/common_helpers.py`.
- У 10 скриптах прибрано 15 локальних копій, виклики тепер `cmn.…`: `verify_premium_spike` (`rd`, `wr`, `ymd(day)`),
  `verify_premarket_inventory` і `verify_premarket_fade` (`rd`, `wr`), `verify_usde_funding` (`day` → `ymd_ms`, `rd`),
  `verify_put_skew`, `verify_weekend_vol`, `verify_dated_basis` (`ymd` → `ymd_ms`), `verify_s3_demo_vs_passport`
  (`_day`), `verify_btc_shock_alt_lag` (`day_str`), `verify_course_step2_early_exit` (`day_of`). Зникли й непотрібні
  `import json` у двох файлах.
- Ці імена ніхто ззовні не бере: `verify_put_skew` вантажать 4 скрипти, але лише `load_snapshots`, `STRESS`, `_ms`;
  `verify_premarket_inventory` — лише `flags_by_base`. Власні `wr` у `verify_usde_funding` і `write_json` у
  `verify_dated_basis` пишуть інакше (без компактних роздільників, `.part`), тому лишились.
- Вивід той самий. Заміни робив скрипт за AST: тіло має збігатися з еталоном, ім'я — траплятися лише як читання (не
  параметр, не присвоєння); `wr` у `verify_premium_spike` передається в `asyncio.to_thread` як посилання, це теж
  замінено. До/після: старі тіла з `HEAD` проти спільних функцій на 105 зразках (мілісекунди з межами доби, float, −1;
  номери днів; JSON з не-ASCII) — усі збіглись, файли `wr` однакові побайтно. Усі 10 скриптів і 4 їхні імпортери
  вантажаться. Тест не додавали: нового хелпера немає.

**Що лишилось для BACKLOG 6.4 (не чіпали):**
- `ymd_ms` під іншими іменами: `verify_market_makers_depth` (`day_of`), `demo_vs_backtest` (`_d`), `sieve` (`day_str`;
  перевірити, хто імпортує). Лишити ім'я (`x = cmn.…`): `verify_liquidation_flush` (`vlf.day_str` і `vlf.read_json`
  беруть `verify_oi_liquidation_flush`, `verify_btc_shock_alt_lag`), `verify_news_events`, `verify_option_modules`,
  `verify_setup_passport`, `verify_course_s14_ema` (`s1.day_of`, `s1.rd` / `s1.wr`), `research/vault_profile`.
- `ymd_day`: `verify_course_s1_spot_alts` (`day_str`), `verify_oi_turnover_short`, `verify_residual_momentum_short_leg`,
  `verify_sector_pair_spread`, `verify_toptrader_divergence`, `verify_weekend_idio_reversal`, `verify_xs_anchor_nearness`,
  `verify_xs_volume_liquidity_factors` (`ymd`); лишити ім'я: `verify_funding_crowding`, `verify_listing_drift`.
- `read_json`: `verify_free_supply_history`, `verify_hl_binance_spread`, `verify_holders_revenue`, `verify_taker_imbalance`,
  `verify_token_unlocks` (`rd`), `verify_tradfi_offhours_reopen` (`read_json`).
- Без змін: 46 локальних `rss_mb` (3 варіанти, 7 модулів реекспортують), однакові `_r` у `calls_hourly.py` /
  `demo_vs_backtest.py`; не зливати варіанти `wr`, `ymd(ms | None)` з `"—"`, `iso` з `%H:%M`, `_usd` / `_pct`.
