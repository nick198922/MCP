**Крок:** постійна картка standing-improvements, новий пункт 80 (з BACKLOG 6.13 б): `collector_health` не бачив колекторів, що ввімкнені за замовчуванням, тож Polymarket мовчав три дні непоміченим.

**Результат:**
• `Spec.default_on`: серії `traders` (`trader_snapshot` за venue), `trader_positions`, `supply`, `atm_term`, `basis_hist`, `funding_hist` (за біржею) тепер перевіряються без запису в `features.env`.
• На живій БД одразу видно дірку: `traders:polymarket(_crypto)` 66 %, `trader_positions:polymarket` 69 % (дані до 02.11); рядок у брифі й блок «Система» підхоплюють це без змін коду.
• `make lint` і `make test` пройшли (4583 passed, 2 skipped).

**Критик:** C — approve ([рев'ю](../../reviews/2026-11-05-collector-health-defaults.md)); не блокує: при `TRADEDESK_COLLECTOR_ENABLED=0` нові серії дадуть хибне «never/low»; `trader_positions` — журнал змін, година без змін виглядає як дірка.

**Далі:** `owner` — на мердж. Пункт 8a лишається за власником/Claude на сервері; пункт (а) BACKLOG 6.13 (`trades` v2) відкладено до потреби.
