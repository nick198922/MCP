# IDEA-20261114-mcp-cleanup: MCP tradedesk — злити дублікати, прибрати непотрібне (Q-20261114-1, А1 Б2)

Статус: accepted (рішення власника 2026-11-14, [Q-20261114-1](../QUESTIONS.md): «А1 Б2 В2 Г1 + картка ончейн» — «Так»)
Пріоритет: власник
Порядок: 1
<!-- proposed → accepted | rejected → testing → confirmed | rejected | inconclusive → built -->

Джерело переліку: [звіт аудиту 14.11](../reviews/2026-11-14-weekly-audit.md), розділ «Доручення власника», і [таблиця інструментів](../reviews/2026-11-14-mcp-inventory.md). **В2:** ончейн і Pendle (`dex_pairs`, `contract_risk`, `chain_metrics`, `pendle_*`) лишаються видимими — власник хоче ончейн-торгівлю (картка [onchain-parity](IDEA-20261114-onchain-parity.md)). **Г1** зроблено Claude 14.11: агентам заборонено 14 інструментів запису (`scripts/nightly_agents.sh`), `scripts/mcp_call.py` ходить у працюючу службу (жива БД) і відмовляє записам.

## Прогрес
Пункти по одному за прогін; кожен — розробник → критик C. Інваріант 5 для кожної зміни: `EXPECTED_TOOLS` у `tests/test_server.py`, таблиця в `README.md`, `docs/PARITY.md`, файл у `docs/changes/`; старі назви — у `docs/changes/` з відповідністю «стара → нова», щоб чат і скрипти не губились. Перед прибиранням — `git grep` по `scripts/`, `src/`, `docs/`, панелі: що викликає інструмент або його функцію.

- [x] 1. **А1, пресети стратегій:** `strategy_straddle`, `strategy_strangle`, `strategy_risk_reversal`, `strategy_iron_condor` → `strategy_payoff` з параметром `preset` (поведінка й числа ті самі — тести-еталони старих інструментів переносяться на `preset`).
- [x] 2. **А1, решта злиттів:** `deribit_atm_term` → `deribit_skew_term`; три `coinalyze_*` → відповідні `perps_*` з параметром `source`; `market_ticker` ↔ `market_tickers`; `carry_paper_status` ↔ `paper_carry_status`; три `range_*` → один. По одному злиттю за прогін, якщо зміна велика.
  - 15.11: deribit_atm_term -> deribit_skew_term done ([зміни](../changes/2026-11-15-mcp-cleanup-atm-term.md): `view="atm"`, 105 → 104 інструменти).
  - 17.11: зроблено: `market_ticker` → `market_tickers` (один символ рядком або списком — `fetch_ticker`), `carry_paper_status` → `paper_carry_status(strategy="manual")`, 104 → 102 ([зміни](../changes/2026-11-17-mcp-merge-ticker-carry-status.md)). Лишились три `coinalyze_*` і три `range_*`.
  - 18.11: три `coinalyze_*` → `perps_oi_history` / `perps_funding_history` / `perps_long_short_ratio` з `source="coinalyze"`, 102 → 99 ([зміни](../changes/2026-11-18-mcp-merge-coinalyze-perps.md), [рев'ю](../reviews/2026-11-18-mcp-merge-coinalyze-perps.md)). Лишились три `range_*`.
  - 18.11: три `range_*` → один `range_detect` з `view="ranges"` (за замовчуванням) / `"setup"` (колишній `range_setup`) / `"scan"` (колишній `range_setup_scan`), 99 → 97 ([зміни](../changes/2026-11-18-mcp-merge-range.md)): типові значення кожного старого інструмента збережено, відповіді побайтно ті самі (19 викликів). Пункт 2 закрито.
- [x] 3. **Б2, прибрати шість:** `carry_paper_open`, `carry_paper_close`, `fib_retracement`, `strategy_backtest_momentum`, `events_seed_load`, `rules_presets_load` (разові дії — скриптом у `scripts/`, якщо ще потрібні). `positions_*` лишаються. Після прибирання — оновити `WRITE_TOOLS` у `scripts/mcp_call.py` і заборони в `scripts/nightly_agents.sh` (тест `tests/test_mcp_call.py` звіряє їх із сервером).
  - 18.11: шість прибрано з MCP, 97 → 91 ([зміни](../changes/2026-11-18-mcp-remove-six.md)); `events_seed_load` / `rules_presets_load` — скрипти в `scripts/`, логіка — функції в `tools/events.py` / `tools/rules.py`; `fib_retracement` і бектест моментуму лишились чистими функціями в `analytics/`; `WRITE_TOOLS` оновлено. **Не закрито:** `scripts/nightly_agents.sh` (агентам правити заборонено) досі забороняє чотири прибрані назви — власнику прибрати їх із рядків 464–465 (до того `test_mcp_call` допускає саме ці чотири зайві назви); `src/tradedesk/tools/backtest.py` більше не реєструється, але лишився в git (агентам видаляти заборонено) — власнику видалити. Питання власнику (одне рішення на все): тепер у продакшені ніщо не імпортує `analytics/backtest.py` і `analytics/retracement.py`, а `Store.carry_paper_insert` / `Store.carry_paper_close` викликають лише тести (книга `strategy="manual"` порожня й не поповнюється) — лишити свідомо (наприклад, `analytics/backtest.py` для `scripts/verify_*`) чи видалити разом із `tools/backtest.py`?
  - 19.11: перевірено на `main` (злиття коміту 4beafbf4): `build_server()` реєструє 91 інструмент, жодного з шести серед них немає; `EXPECTED_TOOLS` — 91. Пункт закрито. Те, що лишається власнику, не блокує пункт: зайві заборони в `scripts/nightly_agents.sh` (рядки 464–465) нешкідливі, `test_mcp_call` їх допускає (`REMOVED_WRITE_TOOLS`); видалити `tools/backtest.py`; відповісти на питання про код, який викликають лише тести (див. вище).

## Критерій «вбити»
Власник скасовує.

## Джерела
Q-20261114-1 і відповідь власника 14.11.
