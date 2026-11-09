**Крок:** p, постійна картка `IDEA-20261102-standing-improvements` (відкритий пункт 8a заблокований, додано пункт 289).

**Результат:**
• 97 тестів «збій / зіпсовані дані» у `tests/test_tool_failure_extra_7.py` для `plan_link`, `rules_test`, `odds_devig`, `fib_retracement`, `option_calc`, `strategy_cash_and_carry`.
• Виправлено: `plan_link` (виняток після запису, Infinity/NaN, `OverflowError` на id 10**20), `rules_test` (`ok=true` з Infinity/NaN і на помилці обчислення), `fib_retracement` (Infinity у драбині), `plan_list` (NaN у `tags`).
• У 6.53 лишається 7 інструментів `strategy_*`. Нове відкрите: id > 2^63 кидає виняток у `positions_close`, `rules_disable`, `journal_close`, `plan_add`; `**` у DSL правил не обмежено.
• Перевірено мною: `make lint` чисто, `make test` 7899 passed, 3 skipped.

**Критик:** approve, блокерів немає (`docs/reviews/2026-11-08-tool-failure-tests-7.md`).

**Далі:** `owner`. Нових питань немає.
