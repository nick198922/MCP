**Крок:** p, картка `mcp-cleanup` (Порядок 1), пункт 3 «прибрати шість інструментів» — картки з Порядком 0 заблоковані (див. «Далі»).

**Результат:**
• З MCP прибрано `carry_paper_open/close`, `fib_retracement`, `strategy_backtest_momentum`, `events_seed_load`, `rules_presets_load`: 97 → 91 інструмент.
• Два завантажувачі стали скриптами `scripts/events_seed_load.py`, `scripts/rules_presets_load.py` (типово пишуть у БД служби, друкують шлях).
• `make lint` і повний `make test`: 10316 passed, 3 skipped.

**Критик (C):** перший раунд — request changes (падав тест із порогом, скрипти писали б не в ту БД); виправлено. Повторної рецензії ще не було.

**Власнику:**
• `scripts/nightly_agents.sh` рядки 464–465: прибрати чотири імена видалених інструментів (мені заборонено правити); пункт 3 лишився `[ ]`.
• Питання в картці: що робити з кодом, який тепер викликають лише тести (`tools/backtest.py` тощо).
• `agents-mcp-access`, пункт 1: підагенти не мають права писати в `.claude/` — питання вже в `QUESTIONS.md`.

**Інші картки:** options-playbook п.4 (черга порожня), profit-search (дані/потужність), structural-edges-depth 8f/8k/8l (чекають дат 2027+), x-traders (є гілка `x-btc-charlie-sample-registration` на рев'ю власника).

**Далі:** `continue` — критик C має повторно переглянути виправлення.
