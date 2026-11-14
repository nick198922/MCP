# IDEA-20261114-mcp-cleanup: MCP tradedesk — злити дублікати, прибрати непотрібне (Q-20261114-1, А1 Б2)

Статус: accepted (рішення власника 2026-11-14, [Q-20261114-1](../QUESTIONS.md): «А1 Б2 В2 Г1 + картка ончейн» — «Так»)
Пріоритет: власник
Порядок: 1
<!-- proposed → accepted | rejected → testing → confirmed | rejected | inconclusive → built -->

Джерело переліку: [звіт аудиту 14.11](../reviews/2026-11-14-weekly-audit.md), розділ «Доручення власника», і [таблиця інструментів](../reviews/2026-11-14-mcp-inventory.md). **В2:** ончейн і Pendle (`dex_pairs`, `contract_risk`, `chain_metrics`, `pendle_*`) лишаються видимими — власник хоче ончейн-торгівлю (картка [onchain-parity](IDEA-20261114-onchain-parity.md)). **Г1** зроблено Claude 14.11: агентам заборонено 14 інструментів запису (`scripts/nightly_agents.sh`), `scripts/mcp_call.py` ходить у працюючу службу (жива БД) і відмовляє записам.

## Прогрес
Пункти по одному за прогін; кожен — розробник → критик C. Інваріант 5 для кожної зміни: `EXPECTED_TOOLS` у `tests/test_server.py`, таблиця в `README.md`, `docs/PARITY.md`, файл у `docs/changes/`; старі назви — у `docs/changes/` з відповідністю «стара → нова», щоб чат і скрипти не губились. Перед прибиранням — `git grep` по `scripts/`, `src/`, `docs/`, панелі: що викликає інструмент або його функцію.

- [ ] 1. **А1, пресети стратегій:** `strategy_straddle`, `strategy_strangle`, `strategy_risk_reversal`, `strategy_iron_condor` → `strategy_payoff` з параметром `preset` (поведінка й числа ті самі — тести-еталони старих інструментів переносяться на `preset`).
- [ ] 2. **А1, решта злиттів:** `deribit_atm_term` → `deribit_skew_term`; три `coinalyze_*` → відповідні `perps_*` з параметром `source`; `market_ticker` ↔ `market_tickers`; `carry_paper_status` ↔ `paper_carry_status`; три `range_*` → один. По одному злиттю за прогін, якщо зміна велика.
- [ ] 3. **Б2, прибрати шість:** `carry_paper_open`, `carry_paper_close`, `fib_retracement`, `strategy_backtest_momentum`, `events_seed_load`, `rules_presets_load` (разові дії — скриптом у `scripts/`, якщо ще потрібні). `positions_*` лишаються. Після прибирання — оновити `WRITE_TOOLS` у `scripts/mcp_call.py` і заборони в `scripts/nightly_agents.sh` (тест `tests/test_mcp_call.py` звіряє їх із сервером).

## Критерій «вбити»
Власник скасовує.

## Джерела
Q-20261114-1 і відповідь власника 14.11.
