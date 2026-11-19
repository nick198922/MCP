**Крок:** постійна картка `standing-improvements`, новий пункт 408 (поповнення координатором): усі картки власника заблоковані (порожня черга плейбуку, питання власнику, часові вікна, пауза сита, FOCUS), тож узято Solana для `contract_risk` з BACKLOG.

**Результат:**
• `contract_risk` приймає `chain="solana"` (GoPlus `solana/token_security`, безкоштовно, без ключа; живий зразок збережено у `tests/fixtures/`).
• 14 перевірок: authorities (freeze / mint / close / permanent delegate), transfer fee / hook, LP lock, топ-10 холдерів, ліквідність / mcap.
• Перевірка `honeypot` на Solana завжди `unknown`, тож `overall` там ніколи не `pass`; порожня відповідь GoPlus (код 7012) = `ok=false`, не «безпечно».
• `make lint` і `make test` (10790 passed, 3 skipped) пройшли в моєму прогоні.

**Критик:** C — спершу request changes (блокер: `overall=pass` без симуляції продажу), після раунду виправлень approve ([рев'ю](../../reviews/2026-11-19-contract-risk-solana-review.md)).

**Далі:** `owner`.
• Після злиття: перевірити інструмент через запущену службу MCP (вона ще на старому коді).
• Відкрите: чому GoPlus дає 7012 для BONK; читання прапорця `malicious_address` — майбутня робота.
• Інших карток власника цього прогону не взято: плейбук (черга порожня), x-traders і agents-mcp-access (питання власнику), profit-search (нових даних нема), structural-edges / demo-setups-depth / short-setups (чекають дати чи угод), book-walls (чекає ввімкнення й першої доби даних).
