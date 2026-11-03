# 2026-11-03 — описи параметрів `strategy_*` (інваріант 5)

Пункт 26 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), BACKLOG 6.6.

- **Описано 69 параметрів у 7 інструментах:** `strategy_straddle`, `strategy_strangle`, `strategy_risk_reversal`, `strategy_iron_condor`, `strategy_cash_and_carry`, `strategy_funding_carry` (`src/tradedesk/tools/strategy.py`) і `strategy_backtest_momentum` (`fee_bps`, `slippage_bps`, `include_curve`; `tools/backtest.py`). Спільні параметри шаблонів (`spot`, `days`, `iv`, `size`, `forward`, `width_pct`, `grid`, `kind`, страйки, премії, `side`) — псевдоніми `Annotated` на рівні модуля, як `EX`/`SYM` у `market.py`.
- **Одиниці й застереження в текстах:** IV десятковим дробом і PoP «risk-neutral»; премії в USD за контракт (Deribit котирує в BTC/ETH — помножити на індекс); `width_pct` — десятковий дріб попри назву (0.3 = ±30 %); у carry-шаблонах `days` впливає лише на PoP, funding і комісії не нараховуються; `fee_bps`/`slippage_bps` у б.п. від номіналу, фліп платить двічі.
- **Поведінка та сама:** схеми всіх `strategy_*` до і після збігаються, якщо прибрати поля `description` (перевірено скриптом через `build_server()` на HEAD і на гілці).
- **Allowlist:** було 174 параметри в 55 інструментах, лишилось **105 у 48**. План завдання був ~40 параметрів і ще одна група, але сама група `strategy_*` — 69 параметрів; ділити її навпіл не став, другу групу не брав.
