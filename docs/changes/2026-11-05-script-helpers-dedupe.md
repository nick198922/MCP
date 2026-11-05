### 2026-11-05 — Скрипти: обгортки `rss()` рахують пам'ять через `common_helpers.rss_mb`

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 123 (технічний борг, залишок
  пункту 75). Лише `scripts/`; `src/`, `execution/`, колектори, форварди й `agents_*` не чіпали.
- У 12 скриптах локальна `def rss() -> str` сама читала `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024`;
  тепер вона форматує `cmn.rss_mb()` (те саме число), а `import resource` прибрано. Текст підпису не змінився:
  `peak RSS N MB` — `verify_market_flow_reader`, `verify_oi_turnover_short`, `verify_residual_momentum_short_leg`,
  `verify_taker_imbalance`, `verify_toptrader_divergence`, `verify_weekend_idio_reversal`,
  `verify_xs_anchor_nearness`, `verify_xs_factor_ic_panel`, `verify_xs_volume_liquidity_factors`; `rss N MB` —
  `verify_pump_fade`, `verify_sector_pair_spread`; `N MB` — `verify_carry_modules`. У `verify_market_flow_reader`,
  `verify_pump_fade` і `verify_carry_modules` додано `import common_helpers as cmn`.
- Тест: `tests/test_common_helpers.py::test_script_rss_labels_use_the_shared_rss_mb` — для кожного з 12 модулів
  `mod.cmn is common_helpers`, `resource` немає, а `rss()` на шести значеннях `ru_maxrss` дає рядок, рівний виразу
  прибраного тіла.
- Пропущено:
  - `verify_carry_a_entry_ban.rss_mb` — `int`, `// 1024`, друкується без `:.0f`: інший вивід, як і в пунктах 75–91;
  - 8 місць, де `ru_maxrss / 1024` стоїть прямо в рядку, без функції (понад ліміт файлів; те саме число):
    `verify_demo_rr_threshold`, `verify_dca_martingale_sieve`, `verify_pm_yes_calibration`, `verify_cycle_regime`
    (`/ 1024.0`), `verify_demo_execution`, `verify_grid_short_vol_regime`, `stage0_condor_forward`,
    `condor_forward_costs` (останні два — скрипти форварду кондора, їх краще не чіпати до 30.11);
  - `vsp.rss_mb`, `dsm.rss_mb`, `s7.rss_mb`, `dl.rss_mb`, `vrs.rss_mb` в інших скриптах — це вже той самий об'єкт
    `common_helpers.rss_mb` (перевіряє `test_scripts_import_the_shared_rss_mb`).
