**Крок:** p/поповнення — усі картки власника заблоковані (див. нижче), тож додано й виконано пункт 437 картки standing-improvements (джерело: рев'ю «не блокує»).
• **Результат:** новий `tests/test_verify_s12_demo_vs_passport.py` (1 тест, без мережі): розділ тіньових планів S12 на `--cutoff` однаковий при різному часі прогону; план, закритий після cutoff, рахується відкритим. Скрипт не змінено.
• **Критик (C, sonnet):** approve ([рев'ю](../../reviews/2026-11-20-s12-cutoff-test.md)).
• **Перевірено мною:** `make lint` чисто; `make test` — 10988 passed, 3 skipped.
• **Заблоковано:** options-playbook п. 4 (черга порожня), agents-mcp-access (правка `.claude/agents/*` і `nightly.md` заборонена жорстким правилом координатора — потрібне рішення власника), x-traders (гілка `x-btc-charlie-sample-registration` чекає власника), profit-search п. 2 (нових ◯ від потужності нема), structural-edges 8f (потрібно ≈ 2028), demo-setups-depth 6 / short-setups-demo (мало угод), book-walls (чекає увімкнення й даних), onchain-parity (ключі), assistant (Telegram/панель — не агентам).
**Далі:** `owner` — готово до перегляду.
