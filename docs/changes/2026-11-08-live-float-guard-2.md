# 2026-11-08 — зіпсовані живі поля, друга п'ятірка (пункт 263)

Інваріант 1 `CLAUDE.md`, продовження [пункту 261](2026-11-08-live-float-guard.md). Кожне місце спершу перевірено по коду: усі п'ять були без захисту від `OverflowError` (число з 300+ цифр), а частина й від NaN / inf.

- `current_funding` (`funding_carry_scan`, `funding_spread_scan`, `carry_paper_open`): `markPrice` з 300+ цифр кидав виняток назовні, NaN / inf ішли як mark. Тепер mark — `null`, у `warnings`: «<біржа>: mark price not a finite number for N symbols, shown as null: …». Те саме `OverflowError` у ставці funding тепер відкидає символ, як NaN.
- `forecast_log._rv30` (завдання `forecast_log`): такий самий close або нескінченний час денного бару Binance зривав запис дня. Тепер бар відкидається, у попередженнях завдання: «rv30:<актив>: ccxt:binance … N bar(s) … dropped».
- `hyperliquid_book`: рівень книги з NaN / inf / 300+-цифровою ціною, обсягом чи кількістю тепер відкидається (раніше NaN / inf ішли в mid і оцінку проковзування), у `warnings`: «hyperliquid <coin> book: N ask/bid level(s) … dropped»; сторона книги не-список більше не кидає `TypeError`.
- `pm_threshold.quote_at` / `last_trade_before` (скрипти перевірок Polymarket): точка чи угода з NaN / inf / 300+-цифровою ціною або нескінченним часом пропускається. Попередження тут немає: це чисті функції без `Builder`, інструментів за ними немає.
- `radar.forward_vols` (блок «Радар» знімка асистента): рядок експірації архіву з нескінченним / 300+-цифровим часом чи днями більше не кидає виняток; він відкидається з записом у `missing` блоку (`forward_vol`). `radar._num` теж не кидає на 300+ цифрах.
- Звичайні дані не змінились: живцем 08.11 — 893 символи Binance USDM і 829 Bybit з mark (нечислових 0, попереджень 0), RV30 BTC 0.359, книга HL BTC 20 / 20 рівнів, `funding_spread_scan` 20 рядків без нових попереджень.
- Тести: `tests/test_live_float_guard_2.py` (по кожному місцю: `10**400`, NaN, inf, `None`, рядок → без винятку, запис у `warnings` / `missing`). Решта списку — BACKLOG 6.53. `execution/` і `collector/` не чіпали.
