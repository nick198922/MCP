### 2026-11-03 — Скрипти: дублікати, шосте коло (`read_json` / `write_json` / `ymd_day`, `rss_mb`)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 37 (продовження
  [п'ятого кола](2026-11-03-scripts-dedup-5.md)).
- У `scripts/common_helpers.py` додано три хелпери — дослівні копії локальних `rd` / `wr` / `ymd` з `verify_*.py`:
  `read_json(p)` (`json.loads(p.read_text())`), `write_json(p, obj)` (каталог створюється, компактний JSON
  `(",", ":")` спершу в `p.with_suffix(".tmp")`, потім `replace`), `ymd_day(day)` (номер дня від епохи →
  `YYYY-MM-DD` UTC; `timezone.utc`, бо модуль вантажить і панель на системному `python3`).
- У 10 скриптах локальні копії прибрано, виклики — `cmn.…` (разом 27 копій):
  `verify_funding_settlement` (`rss_mb`, `rd`, `wr`, `ymd`), `verify_spot_perp_flow` (ті самі 4),
  `verify_alt_downside_beta` (`rd`, `wr`, `ymd`), `verify_market_makers_depth` і `verify_hodler_step0` (`rss_mb`,
  `rd`, `wr`), `verify_alt_basket_leverage` (`rss_mb`, `rd`, `ymd`), `verify_post_listing_midage` (`rss_mb`, `ymd`),
  `verify_carry_a_bybit_inventory` і `verify_hl_spot_perp_carry` (`rss_mb`, `rd`), `verify_xs_factor_ic_panel` (`ymd`).
  Брали лише тіла, що збігаються символ у символ (`tz=UTC` і `UTC` — той самий об'єкт); ці модулі ніхто не імпортує.
  `wr` у `verify_hl_spot_perp_carry` (без `mkdir`, без компактних роздільників) і його `ymd(ms | None)` лишились.
- Вивід той самий: тіла не змінились, `ruff` (F821) підтвердив, що всі виклики знайшли нове ім'я. Живого прогону до/після
  не робили (скрипти тягнуть архіви бірж); натомість тести.
- Тести: `tests/test_common_helpers.py` — `write_json` (байт у байт той самий текст, каталог, перезапис, ім'я `.tmp`),
  `read_json` (туди-назад, нема файлу, битий JSON), `ymd_day` (0, 2024-01-01, 29 лютого, −1), і всі 10 скриптів
  імпортуються без власних копій (`rd` / `wr` / `ymd` / `rss_mb` / `resource`).

**Що лишилось для BACKLOG 6.4 (не чіпали):**
- `rss_mb`: 49 копій `/ 1024` і 3 варіанти (`/ 1024.0` у `verify_pm_h7_qlike`, `verify_option_modules`; `int` у
  `verify_carry_a_entry_ban`); 7 модулів, з яких його беруть інші скрипти, — перелік у [п'ятому колі](2026-11-03-scripts-dedup-5.md).
- Однакові `rd` / `wr` / `ymd(day)`: `verify_premarket_inventory` (його вантажить `verify_premarket_fade`, але без
  `rd` / `wr`), `verify_perp_launch_existing`, `verify_usde_funding`
  і `verify_taker_imbalance` (лише `rd`), `verify_xs_anchor_nearness`, `verify_residual_momentum_short_leg`,
  `verify_xs_volume_liquidity_factors` (лише `ymd`); у `verify_funding_crowding` (`fc.`), `verify_news_events` (`v0.` /
  `nev.`), `verify_listing_drift` (`ld.`) їх беруть інші скрипти — лишити ім'я (`rd = cmn.read_json`).
- `ymd(ms)` (`ms / 1000`, однакове тіло) — ~10 копій (`verify_news_events`, `verify_weekly_straddle`, `verify_put_skew`,
  `verify_event_straddle`, `verify_daily_iv_har`, `verify_dated_basis`, …): кандидат на `ymd_ms` наступним колом.
- Інші: `wr` без `mkdir` / без компактних роздільників / з `p.suffix + ".tmp"` (`verify_hl_spot_perp_carry`,
  `verify_hl_binance_spread`, `verify_holders_revenue`, `verify_usde_funding`, `verify_free_supply_history`) — інший
  текст файлу, не зливати; `_r` у `calls_hourly.py` / `demo_vs_backtest.py`; форматери `_usd` / `_pct` — тексти різні.
