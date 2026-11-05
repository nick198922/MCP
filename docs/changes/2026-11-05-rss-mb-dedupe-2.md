### 2026-11-05 — Скрипти: друга партія локальних `rss_mb` → `common_helpers.rss_mb` (BACKLOG 6.4)

- Картка [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), пункт 87 (продовження пункту 75,
  [перша партія](2026-11-05-rss-mb-dedupe.md)). Лише `scripts/verify_*.py`; `src/`, `execution/`, колектори,
  форварди й `agents_*` не чіпали; числа й поведінка скриптів не змінились.
- У 15 скриптах копію `def rss_mb()` з тілом `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024` (рядок у
  рядок як у `common_helpers.rss_mb`) замінено на `from common_helpers import rss_mb` (з `# noqa: E402` там, де
  імпорти стоять після `sys.path.insert`), `import resource` прибрано. Ім'я лишилось атрибутом модуля, тож
  `vsp.rss_mb` (скрипти `verify_s*_modules`, і в робочих процесах `Pool`), `vrs.rss_mb` (`verify_seller_exhaustion`)
  і `from verify_session_stop_hunt import rss_mb` (`verify_session_stop_hunt_fx`) працюють як раніше. Змінені:
  `verify_per_asset_recheck`, `verify_pm_dislocation_reversion`, `verify_pm_favorites`,
  `verify_pm_longdated_tail_no`, `verify_pre_event_vol`, `verify_premarket_fade`, `verify_premarket_inventory`,
  `verify_regime_side`, `verify_risk_overlay`, `verify_session_stop_hunt`, `verify_setup_passport`,
  `verify_smart_money_polymarket_h1`, `verify_smt_divergence`, `verify_tradfi_offhours_reopen`,
  `verify_usde_funding`.
- Тест: ці 15 модулів додано до `RSS_MB_IMPORTED` у `tests/test_common_helpers.py` (`rss_mb is common_helpers.rss_mb`,
  немає `resource`).
- Лишилось 7 локальних `rss_mb`, не чіпали (інша поведінка або обгортка):
  - інше тіло — 3: `verify_option_modules`, `verify_pm_h7_qlike` (`/ 1024.0`), `verify_carry_a_entry_ban`
    (`-> int`, `// 1024`, локальний `import resource`);
  - обгортки `return v0.rss_mb()` — 4: `verify_news_events_0b`, `_0c`, `_0d`, `verify_market_makers_news`.
- Перевірка: `make lint`, `uv run pytest -q tests/test_common_helpers.py` (81 passed), `make test` (4605 passed,
  2 skipped), `python -m py_compile` кожного з 15 скриптів.
