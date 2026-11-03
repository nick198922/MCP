### 2026-11-03 — Скрипти: дублікати, сьоме коло (`ymd_ms`, `rss_mb`, `rd` / `wr`)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 38 (продовження
  [шостого кола](2026-11-03-scripts-dedup-6.md)).
- У `scripts/common_helpers.py` новий хелпер `ymd_ms(ms)`: мілісекунди епохи → `YYYY-MM-DD` UTC. Тіло дослівно
  як у локальних `ymd(t)` (`datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")`; `tz=UTC` і `timezone.utc` —
  той самий об'єкт).
- У 10 скриптах локальні копії прибрано, виклики тепер `cmn.…` (разом 20 копій): `verify_perp_launch_existing`
  (`rss_mb`, `ymd`, `rd`, `wr`), `verify_funding_interval_cut`, `verify_coinm_funding`, `verify_dvol_filtered_straddle`,
  `verify_assistant_recs`, `verify_daily_iv_har`, `verify_vrp_real_marks`, `verify_strangle_sell_far` (`rss_mb`, `ymd`),
  `verify_event_straddle` і `verify_weekly_straddle` (`ymd`). Брали лише тіла, однакові символ у символ. `ymd`, `rd`,
  `wr` чи `rss_mb` цих модулів інші скрипти й тести не беруть: `verify_weekly_straddle` вантажить
  `verify_dvol_filtered_straddle` (`vws.`), але без `ymd`. Власний `write_json` у `verify_coinm_funding` пише JSON без
  компактних роздільників, тож він лишився.
- Вивід той самий: тіла не змінились. Заміни робив скрипт, і він зупинявся, якщо копія не збігалась дослівно або
  ім'я траплялось не як виклик; `ruff` (F821) підтвердив, що всі виклики знайшли нове ім'я. Живого прогону до/після
  не робили (скрипти тягнуть архіви бірж), натомість є тести.
- Тести: `tests/test_common_helpers.py` перевіряє `ymd_ms` (0, межа доби, 29 лютого 08:00, float, −1; збіг із формою
  старих копій, з `tz=` і без, на 8 значеннях), а також те, що всі 10 скриптів імпортуються без власних копій і без
  `resource`.

**Що лишилось для BACKLOG 6.4 (не чіпали):**
- Однакові з `ymd_ms` тіла під іншими іменами, до 10 файлів на коло: `verify_put_skew`, `verify_weekend_vol`,
  `verify_dated_basis` (`ymd`), `verify_s3_demo_vs_passport` (`_day`), `demo_vs_backtest` (`_d`), `verify_usde_funding`
  (`day`), `verify_liquidation_flush`, `verify_btc_shock_alt_lag`, `sieve` (`day_str`), `verify_course_step2_early_exit`,
  `verify_market_makers_depth` (`day_of`). Перед заміною перевірити, хто їх імпортує. Там, де ім'я беруть інші скрипти
  чи тести, лишити його як `ymd = cmn.ymd_ms`: `verify_news_events` (`v0.ymd`), `verify_option_modules` (`vom.ymd`),
  `verify_setup_passport` (`vsp.iso`), `verify_course_s14_ema` (`s1.day_of`), `research/vault_profile` (`vp.ymd` у
  тестах). `radar_backfill.iso` — спершу перевірити, чи він не періодичний.
- Не зливати: `ymd(ms | None)` з `"—"` (`verify_hl_spot_perp_carry`, `verify_hl_binance_spread`, `research/probe_hl_depth`),
  `iso` з `%H:%M` у `research/probe_*`, `ymd` через `date_of` у `verify_pre_event_vol`.
- `rss_mb`: ще 46 локальних визначень (серед них 3 варіанти: `/ 1024.0`, `int`), 7 модулів реекспортують його іншим
  скриптам (перелік у [п'ятому колі](2026-11-03-scripts-dedup-5.md)).
- З шостого кола без змін: однакові `rd` / `wr` / `ymd(day)` у `verify_premarket_inventory`, `verify_usde_funding`,
  `verify_taker_imbalance`, `verify_xs_anchor_nearness`, `verify_residual_momentum_short_leg`,
  `verify_xs_volume_liquidity_factors`, `verify_sector_pair_spread`, `verify_premium_spike`, `verify_toptrader_divergence`,
  `verify_oi_turnover_short`, `verify_weekend_idio_reversal` (у `verify_funding_crowding`, `verify_news_events`,
  `verify_listing_drift` лишити ім'я). Не зливати: варіанти `wr`, `_r` у `calls_hourly.py` / `demo_vs_backtest.py`
  (пара однакова, решта різні) і форматери `_usd` / `_pct`.
