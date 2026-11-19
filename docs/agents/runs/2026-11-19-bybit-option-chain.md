**Крок:** p (картки власника): усі картки Порядку 0 заблоковані (agents-mcp-access — Q-20261114-2 і заборона правити `.claude/`; options-playbook — черга правил порожня; x-traders — відкрита гілка btc_charlie; profit-search п. 2 — 7 порожніх партій; 8f H11 — вікно до 2027-02-24; book-walls п. 1 — лишились дії Claude на сервері). Standing-improvements мала 0 незаблокованих пунктів → поповнення з BACKLOG.
**Результат:**
• Новий інструмент `bybit_option_chain` (read-only): ланцюг опціонів Bybit BTC/ETH — bid/ask, спред у % і в в.п. IV, mark IV, OI; компактна відповідь за замовчуванням (≈ 5.5 тис. символів), повна — `include_details=true`.
• Перевикористано `option_xvenue`; тести: happy-path, збій провайдера, порожній ланцюг, USDC.
• Фікстура — демо-хост Bybit (23.10), не mainnet; живу перевірку не робили.
• `make lint` і `make test` (10806 passed, 3 skipped) — мої.
**Критик:** C — approve, без блокерів ([рев'ю](../../reviews/2026-11-19-bybit-option-chain.md)); нотатки: ETH лише порожня відповідь, перехрещена книга не обробляється.
**Далі:** `owner`. Власнику перевірити живцем: `uv run python scripts/mcp_call.py bybit_option_chain '{"currency":"BTC","min_days":7,"max_days":24}'`.
