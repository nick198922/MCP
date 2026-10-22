# 2026-10-22 — демо-трейдер: лімітні входи й цілі, ринок лише за потреби

- Запит власника 22.10: ринкові ордери дорожчі, тож входи й виходи — лімітками, а по ринку закривати лише тоді, коли цього вимагає ситуація.
- Нове: `src/tradedesk/execution/orders.py` — чисті рішення `entry_step` і `exit_step`: чекати, переставити, ринок або зняти. Пороги: 0.25R від плану, 10 хв на вхід, 5 хв на вихід за часом, 0.25R до стопу.
- `execution/bybit_demo.py`: `quote`, post-only у `place_bracket(price=…)`, `place_reduce_limit`, `amend`, `cancel`, `set_take_profit`.
- `execution/demo_trader.py`: статуси `pending` / `missed`; щохвилинний `manage` веде лімітки входу й виходу за часом; ціль — reduce-only лімітка; після закриття знімає ордери, що лишились; у повідомленні про закриття — комісії.
- Схема 21: нові колонки `demo_trade` (`entry_order`, `entry_px`, `entry_kind`, `tp_link`, `exit_order`, `exit_px`, `exit_ts`, `exit_kind`, `fees_usd`, `note`).
- Тести: `tests/test_execution_orders.py` (чисті рішення), `tests/test_demo_trader.py` (переставлення, добір по ринку, частковий вхід, `missed`, post-only, знятий біржею, відмова лімітки цілі, вихід за часом лімітом і ринком, комісії).
- Стоп-маркет, guard і ліміти не змінювались. Ризик і R:R guard рахує з комісією тейкера — консервативно.
