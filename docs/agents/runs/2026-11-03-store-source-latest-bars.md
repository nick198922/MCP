**Крок:** p — постійна картка `IDEA-20261102-standing-improvements`, пункт 29 (бюджет 90 %, лише крок p).
**Результат:**
• `range_detect` / `range_setup` / `market_regime` з `source="store"` тепер беруть найновіші N барів (`Store.ohlcv_range(newest=True)`), раніше — найстаріші.
• Тести на БД із 1000 барів і N=100 (store, обидва range-інструменти, regime); `make lint` і `make test` — 4216 passed.
• Описи `lookback_bars` уточнено (store: найновіші N рядків).
**Критик:** C, approve ([рев'ю](../../reviews/2026-11-03-store-source-latest-bars.md)); дрібниця: опис `RANGE_LOOKBACK` у `range_setup_scan` (лише live) згадує store.
**Далі:** owner. Відкритих пунктів у картці не лишилось — наступний прогін додасть з «Як поповнювати».
