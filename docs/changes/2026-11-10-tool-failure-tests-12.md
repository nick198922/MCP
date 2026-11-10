# 2026-11-10 — тести «зіпсовані дані → відповідь, не виняток», частина 12 (пункт 300)

Інваріанти 1 і 3 `CLAUDE.md`, два останні відомі залишки BACKLOG 6.53 після пункту 299.

- Новий `tests/test_tool_failure_extra_12.py` (58 тестів, без мережі).
- **`analytics/pm_threshold.history_alignment`:** угода чи точка `/prices-history` з текстом, None, inf / NaN, 300+-значним цілим, без ключа або не-словник кидала `ValueError` / `TypeError` / `OverflowError` (і валила `scripts/verify_pm_threshold.py`), а ціна 1e308 давала `mean_abs_to_prev = inf`. Тепер такий рядок (і ціна поза [0, 1]) пропускається; у результаті нові лічильники `skipped_trades` / `skipped_points`, решта рахується як раніше. Час 1e308 / 10**400 — валідне ціле, рядок лишається.
- **Метрики DSL правил** (`rules/metrics.py`): текст / blob в останньому рядку (`funding`, `atm_iv`, `rr25`, `basis`, `basis_annualized`) і відкинуті рядки вікна (`funding_mean` / `funding_zscore`, `atm_iv_mean` / `atm_iv_zscore`) тепер записуються в `MetricContext.notes`. `rules_test` віддає їх у `warnings` (без тексту з бази, по одному на метрику); значення, як і раніше, null / falsy.
- **Задача алертів** (`alerts/engine.py`): DSL-правило на такому рядку пише попередження в лог один раз на правило за процес. Знайдено під час перевірки: JSON-правило (`evaluate_rules`) порівнювало рядок з порогом і кидало `TypeError` з усієї задачі; тепер це «немає значення» з попередженням у лозі, інші правила спрацьовують.
- `collector/`, `execution/`, `core/`, `providers/` не чіпали.
