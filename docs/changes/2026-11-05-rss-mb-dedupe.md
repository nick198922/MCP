### 2026-11-05 — Скрипти: локальні `rss_mb` → `common_helpers.rss_mb` (BACKLOG 6.4)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 75. Лише `scripts/`; `src/`,
  `execution/`, колектори, заморожені форварди й `agents_*` не чіпали.
- У 25 скриптах копію `def rss_mb()` з тілом `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024` (рядок у
  рядок як у `common_helpers.rss_mb`) замінено на `from common_helpers import rss_mb`; `import resource` прибрано
  всюди (ruff F401). Виклики `rss_mb()` не змінено, ім'я лишилось атрибутом модуля, тож `dl.rss_mb`, `dsm.rss_mb`,
  `s7.rss_mb`, `base.rss_mb`, `v0.rss_mb` в інших скриптах працюють як і раніше. Змінені:
  `verify_btc_shock_alt_lag`, `verify_carry_a_delisting`, `verify_catalyst_scalp`, `verify_course_s14_ema`,
  `verify_course_s1_spot_alts`, `verify_course_step2_early_exit`, `verify_course_step3_s18`,
  `verify_course_step4_fvg_ob`, `verify_course_step5_s17`, `verify_course_step6_s13`, `verify_course_step7_basics`,
  `verify_course_step8_macro_drift`, `verify_demo_context_filters`, `verify_demo_stop_management`,
  `verify_expiry_pin_mirror`, `verify_gex_taker_flow`, `verify_macro_b1b`, `verify_macro_regimes`,
  `verify_macro_release`, `verify_macro_slow`, `verify_mark_stop`, `verify_market_makers_onchain`,
  `verify_news_events`, `verify_ny_midnight_open`, `verify_oi_liquidation_flush`.
- Тест: `tests/test_common_helpers.py::test_scripts_import_the_shared_rss_mb` — у кожному з 25 модулів
  `rss_mb is common_helpers.rss_mb` і немає `resource`.
- Пропущено (лишилось 22 локальні `rss_mb`):
  - ліміт 25 файлів, однакове тіло — 15: `verify_per_asset_recheck`, `verify_pm_dislocation_reversion`,
    `verify_pm_favorites`, `verify_pm_longdated_tail_no`, `verify_pre_event_vol`, `verify_premarket_fade`,
    `verify_premarket_inventory`, `verify_regime_side` (`vrs.rss_mb`), `verify_risk_overlay`,
    `verify_session_stop_hunt`, `verify_setup_passport` (`vsp.rss_mb`), `verify_smart_money_polymarket_h1`,
    `verify_smt_divergence`, `verify_tradfi_offhours_reopen`, `verify_usde_funding`;
  - інше тіло — 3: `verify_option_modules`, `verify_pm_h7_qlike` (`/ 1024.0`), `verify_carry_a_entry_ban`
    (`int`, `// 1024`, локальний `import resource`);
  - обгортки `return v0.rss_mb()` — 4: `verify_news_events_0b`, `_0c`, `_0d`, `verify_market_makers_news`.
- У BACKLOG 6.4 стояло 46; фактично до зміни було 47 (рахував Grep-ом `def rss_mb` у `scripts/`, без
  `common_helpers.py`).
