**Крок:** p, постійна картка `IDEA-20261102-standing-improvements`, новий пункт 21 (технічний борг: інваріант 5). Бюджет тижня 88 %, тому лише крок p.

**Результат:**
• Аудит 108 інструментів: 21 викликає провайдера без тесту збою.
• Додано 9 тестів для 8 прогалин (`tests/test_provider_failures.py`): Deribit, Hyperliquid, CoinGecko, Polymarket. Збій → `ok=false` і запис у `missing`, без винятку назовні.
• Порушень інваріанта 1 не знайдено, код інструментів не змінено.
• Лишилось 13 прогалин (опціони Deribit, market_*, мультибіржові perps_*): у `docs/changes/` і `docs/BACKLOG.md` (6.5).
• Мій прогін: `make lint` чистий, `make test` 4180 passed, 2 skipped.

**Критик:** C (sonnet): approve ([рев'ю](../../reviews/2026-11-02-provider-failure-tests.md)). Дрібниця: `_server_with_down` збирає `Runtime` окремо від фікстури `server`.

**Далі:** owner. Пункт 8 (`last_failure` колекторів) чекає деплою. Інші картки з пріоритетом заблоковані часом чи рішенням власника.
