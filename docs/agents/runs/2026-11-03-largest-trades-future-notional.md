**Крок:** p, постійна картка `IDEA-20261102-standing-improvements`; відкритих пунктів без блокувань не було (лишився 8a), тож додано пункт 30 з BACKLOG. Бюджет тижня 90 %, тому лише цей крок.

**Результат:**
• `deribit_largest_trades` з `kind="future"`: `notional_usd = amount` (USD), раніше `amount * price` завищувало номінал у ціну BTC разів. Опціони без змін.
• Тест happy-path на фікстурі `tests/fixtures/deribit_futures_trades_2026-11-03.json`, випадок збою провайдера для `future`, опис інструмента з одиницями.
• Розробник заявив про жваву перевірку Deribit (BTC і ETH, `amount/contracts` = 10 і 1 USD). Критик відтворив лише BTC; я сам не перевіряв. Власнику бажано один раз глянути `curl` на futures-угоди.
• `make lint` і `make test` у мене: 4218 passed, 2 skipped.

**Критик:** C, approve ([рев'ю](../../reviews/2026-11-03-largest-trades-future-notional.md)); дрібниці: лінійні USDC не охоплені, `count` для futures не тестується.

**Далі:** `owner`. Пункт 8a (причина збоїв WebSocket Bybit/OKX) чекає даних сервера. Нових питань немає.
