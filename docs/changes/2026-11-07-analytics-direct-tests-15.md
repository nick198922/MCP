# 2026-11-07 — прямі тести для 9 функцій `analytics/` за суворою перевіркою

Пункт 252 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), [BACKLOG 6.9](../BACKLOG.md).

- **Покрито всі 9** функцій, які лишала сувора перевірка (ім'я як ідентифікатор у коді тестів): новий `tests/test_analytics_direct_15.py`, 20 тестів, `src/` не змінено.
  - `dex.num`, `option_xvenue.num`: рядкові числа, знак, `NaN` / `inf` / `"1e400"`, биті типи → `None`; у `option_xvenue.num` ще нуль і від'ємні → `None`.
  - `expiry_pin.distance` (і `in_band` на ньому): |K − S| / S вручну, симетрія, межі включно.
  - `macro_regimes.rolling`: ручні суми вікна, пропуски в будь-якій колонці викидають рядок, `min_valid`, каузальність (майбутнє значення не змінює минулих).
  - `option_replay.atm_strike`: дельта з Black-76 (F = K = 100, 30 днів, σ = 0.5), рівновіддалені страйки → нижчий, межі смуги включно, без запасного страйка.
  - `options.npdf`: φ(0) = 1/√(2π), φ(1), φ(2), парність, інтеграл = 1, похідна `ncdf`, хвости → 0.
  - `pm_gap_report.primary_early_payout`: ручні лічильники й середнє другого варіанта CLV +24 h (як у тесті `close_early_payout`), `n/a` поза обома варіантами, `int`-ідентифікатори ринків і рядковий `closed_ts`.
  - `hull_suite_flip.wma`: ваги n…1 вручну, перезапуск сегмента, `NaN` псує рівно ті вікна, де він є.
  - `smart_money.mark_price`: |value_usd| / size, нульовий чи порожній розмір → `None`.
- **Помилок, що змінюють числа, не знайдено.** Зауваження без правки (поведінку закріплено тестами як є): `dex.num` і `option_xvenue.num` кидають `OverflowError` на цілому числі понад діапазон float (`10**400`), бо ловлять лише `TypeError` / `ValueError`; з JSON таке приходить лише як ціле з 300+ цифр, `"1e400"` дає `inf` і обробляється. `expiry_pin.distance` без захисту від spot = 0 (`ZeroDivisionError`), викликачі передають додатний spot. `smart_money.mark_price` без `value_usd` дає `0.0`, а не `None`.
