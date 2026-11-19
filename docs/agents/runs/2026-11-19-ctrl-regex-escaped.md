**Крок:** крок p (картки власника) — усі картки з «Порядок 0/1» заблоковані або без нових правил у черзі; взято поповнення постійної картки standing-improvements (пункт 407, рев'ю «не блокує»).

**Результат:**
• Спільний `CTRL_RE` в `analytics/_text.py` з іменованими екранами замість буквальних невидимих символів; `yields.py` і `pendle.py` беруть його звідти, поведінка `_clean` та сама (81 символ, збігається з попереднім набором).
• Охоронець `tests/test_no_invisible_chars.py`: у `src/**/*.py` немає символів категорії Cf / bidi.
• `make lint` і `make test` зелені (10761 passed, 3 skipped).

**Критик:** C (sonnet) — approve ([рев'ю](../../reviews/2026-11-19-ctrl-regex-escaped.md)); зауваження не блокують (охоронець лише для `.py`, у `tests/` лишилось ~13 файлів з буквальними символами).

**Чому не інші картки:** options-playbook п. 4 — у черзі нема нового правила (4.11); agents-mcp-access — потребує правки `.claude/agents/*`, що забороняє інструкція (питання власнику); x-traders — чекає гілку `x-btc-charlie-sample-registration`; profit-search п. 2 — кандидатів нема; structural-edges 8f — ворота потужності, ≈ 2028; book-walls — чекає вмикача на сервері й даних; demo-setups / short-setups — чекають угод; sieve на паузі.

**Далі:** `owner`. Питання власнику: чи дозволити агентам дописати `mcp__tradedesk` у `tools:` визначень `.claude/agents/*` (картка agents-mcp-access, п. 1).
