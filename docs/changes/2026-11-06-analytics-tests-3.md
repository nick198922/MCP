# 2026-11-06 — тести ще для 10 функцій `analytics/`

Пункт 172 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), [BACKLOG 6.9](../BACKLOG.md).

- **Перелік:** окремого скрипта аудиту в `scripts/` немає, тож функції без згадки в `tests/` пораховано розбором AST (верхньорівневі публічні функції `analytics/`, ім'я як слово в `tests/`), як у попередніх колах. Після змін лишилось 74 із 2171 (критик перерахував).
- **Покрито 10** (новий `tests/test_analytics_direct_6.py`, 17 тестів, еталони пораховано вручну в коментарях):
  - `carry.carry_observers` і `carry.spread_observers` (колбеки спостережень для форвардів carry A / B: виплати funding з ціною перпа на момент виплати, максимум / мінімум барів, APR = сума funding × 365 д / вікно, `None` для застарілої ціни й непокритої історії);
  - `funding_crowding.target_weights` і `basket_components` (±0.5 валового на сторону, масштаби, повтор символу; ціна й funding кошика);
  - `dated_basis.last_friday` (08:00 UTC, місяць, що закінчується п'ятницею, грудень, високосний лютий);
  - `strategy_sieve.symbol_seed` (еталон через стандартне значення crc32 від `123456789`) і `funding_r` (історія / стрес-плоский / без funding, знак за напрямком);
  - `smart_money.signed_notional`, `pm_modules.tau_of_hours` (T = години / (365 × 24)), `collector_health.fmt_bytes`.
- Помилок не знайдено, код `src/` не змінено.
- Лічильник у BACKLOG 6.9: **лишилось 74 із 2171** (перераховано; між пунктами 127 і 172 лічильник не вели).
