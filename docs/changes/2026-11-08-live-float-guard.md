# 2026-11-08 — голий `float()` / `int()` на живих полях бірж (пункт 261)

Інваріант 1 `CLAUDE.md`. Разовий AST-скрипт (не в git) знайшов у `tools/` і `analytics/` перетворення полів відповідей без захисту від `None`, NaN, inf, нечислового рядка та `OverflowError` (300+-цифрове число в JSON). Виправлено п'ять місць, що живлять форварди, carry і Polymarket:

- `assistant_paper_status` і `forecast_log_status`: ціна поставки Deribit `NaN` / `Infinity` закривала угоду чи прогноз назавжди на не-числі, 300+-цифрова кидала виняток. Тепер такий рядок іде в `missing` (`delivery_prices:<валюта>`, `parse_error`), угода / прогноз лишаються відкритими.
- `carry_paper_open` / `carry_paper_status`: `last` тікера `Infinity` оцінював пару за нескінченною ціною. Тепер він не береться, у `warnings` пишеться «<біржа> <символ>: last price in the ticker is not a finite number»; вхід бере mark із funding, позначка пари — `missing`.
- `plan_review` і завдання `plan_resolve`: час свічки-текст / NaN / inf кидав виняток назовні, а NaN у close давав R = NaN. Тепер такі свічки відкидаються з попередженням `ccxt:<біржа> <символ> <tf>: N candle(s) ... dropped`.
- Сканер `pm_gap` (форвард Polymarket ↔ опціони): нечисловий `feeSchedule.rate` з Gamma кидав `ValueError` зі `scan_gaps`. Тепер ринок відкидається зі статусом `fee` (у `warnings`: `markets left out: fee=N`), нечисловий обсяг рахується як 0, а `OverflowError` у барах Binance, книзі CLOB і `outcomePrices` більше не вилітає.
- Живцем 08.11: 50 цін поставки BTC, 50 годинних барів Binance, `last` з Bybit і Binance USDM та 669 ринків Gamma (96 придатних, комісія 0.07 / 1) розібрано так само, як раніше; статусу `fee` на справжніх ринках немає.
- Тести: по одному на кожне виправлення (зіпсоване поле → відповідь без винятку, запис у `warnings` / `missing`). Решта списку — BACKLOG 6.53. `execution/` і `collector/` не чіпали.
