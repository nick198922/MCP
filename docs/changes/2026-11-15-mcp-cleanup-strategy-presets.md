# 2026-11-15 — пресети стратегій у `strategy_payoff` (картка mcp-cleanup, пункт 1)

Рішення власника Q-20261114-1 (А1), картка [`IDEA-20261114-mcp-cleanup`](../ideas/IDEA-20261114-mcp-cleanup.md). Чотири інструменти-шаблони прибрано з MCP; ті самі структури тепер будує `strategy_payoff` з параметром `preset`. Інструментів було 109, стало 105.

## Стара → нова

Аргументи ті самі й з тими самими назвами; додається лише `preset`. Відповідь та сама: ноги в `data.legs`, блок виплати / грецьких / PoP, `kind` за замовчуванням `summary`.

| Було | Стало |
|---|---|
| `strategy_straddle(strike, call_premium, put_premium, spot, days, iv, side?, size?, forward?, width_pct?, grid?, kind?)` | `strategy_payoff(preset="straddle", strike, call_premium, put_premium, spot, days, iv, side?, size?, …)` |
| `strategy_strangle(call_strike, put_strike, call_premium, put_premium, spot, days, iv, side?, size?, …)` | `strategy_payoff(preset="strangle", call_strike, put_strike, call_premium, put_premium, spot, days, iv, side?, size?, …)` |
| `strategy_risk_reversal(call_strike, put_strike, call_premium, put_premium, spot, days, iv, direction?, size?, …)` | `strategy_payoff(preset="risk_reversal", call_strike, put_strike, call_premium, put_premium, spot, days, iv, direction?, size?, …)` |
| `strategy_iron_condor(long_put_strike, short_put_strike, short_call_strike, long_call_strike, long_put_premium, short_put_premium, short_call_premium, long_call_premium, spot, days, iv, size?, …)` | `strategy_payoff(preset="iron_condor", ті самі вісім страйків і премій, spot, days, iv, size?, …)` |

`…` — `forward`, `width_pct`, `grid`, `kind`, як і раніше. Типові значення ті самі: `side="long"`, `direction="bullish"`, `size=1`, `kind="summary"` (для власних `legs` `kind` за замовчуванням, як і раніше, `detailed`).

## Що змінилось у поведінці

- Числа ті самі: пресет будує ноги тими самими функціями `analytics/strategy.py` (`build_straddle` / `build_strangle` / `build_risk_reversal` / `build_iron_condor`) і рахує тим самим `_packed`. Тест `test_preset_matches_the_former_template_tool` звіряє відповідь пресета з тим, що повертав старий інструмент, і з `strategy_payoff` на тих самих ногах; значення зі старих тестів (стреддл 83000 / 1500 + 1500: max loss −3000; кондор 80 / 90 / 110 / 120 з кредитом 4.5: max profit 4.5, max loss −5.5) перевіряються на `preset`.
- Нове: `ok=false` (`invalid_input`) замість відповіді, коли немає ні `legs`, ні `preset`; коли є і те, і те; коли пресету бракує обов'язкового параметра (у повідомленні — яких саме); коли передано параметр, що не належить пресету (наприклад, `side` для `risk_reversal`), або параметр пресета без `preset`. Неправильний порядок страйків, як і раніше: `missing: strikes` (`data_unavailable`). Невідомий `preset` відхиляє схема MCP.
- Внутрішні функції не видалено: `build_*` у `analytics/strategy.py` лишаються (на них тримаються пресети й тести).

## Хто згадував старі назви

- Оновлено: `src/tradedesk/prompts/pre_trade_checklist.md` (промпт для моделі), `docs/knowledge/options.md`, `docs/knowledge/options-playbook.md`, `README.md`, `docs/PARITY.md`, тести (`test_server`, `test_strategy`, `test_tool_failure_extra_9`, `test_calc_degenerate_inputs`, `test_tools_never_raise`, `test_tools_envelope_shape`).
- Не викликали: `scripts/` (зокрема `mcp_call.py`, `WRITE_TOOLS`), `scripts/nightly_agents.sh`, `.claude/`, панель (`scripts/dashboard.py`). Старі назви лишились лише в історичних записах (`CHANGELOG.md`, `docs/history/`, `docs/changes/`, `docs/reviews/`, `docs/agents/runs/`, журнал пункту 6.53 у `docs/BACKLOG.md`) і в картках ідей — їх не переписуємо.
- `CLAUDE.md` досі каже «109 шт.» — застаріло, правка за власником.
