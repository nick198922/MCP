# Трекер закликів: звіт

Звіт на 2026-10-23 (`scripts/calls_check.py`; денні свічки перпів, лише завершені дні UTC). Заклики: [`calls.csv`](calls.csv); правила оцінки: [`src/tradedesk/analytics/calls.py`](../../src/tradedesk/analytics/calls.py).

Як читати:
- «Результат» — дохідність угоди від входу в зоні до цілі, стопу або останнього закриття, у напрямку заклику.
- «Одразу» — якби діяли в день після заклику, не чекаючи зони. Заклик чогось вартий, якщо результат стабільно кращий.

## Підсумок за джерелами

| Джерело | Закликів | З даними | Дійшло до зони | Ціль | Стоп | Сер. результат | Сер. «одразу» |
|---|---|---|---|---|---|---|---|
| LIB-20261014-ai-narrative | 6 | 6 | 3 | 0 | 0 | +5.2 % | -5.2 % |
| LIB-20261014-rwa-narrative | 9 | 9 | 2 | 0 | 0 | -2.6 % | -22.0 % |
| LIB-20261015-doubletop-stream | 6 | 6 | 4 | 0 | 0 | +1.8 % | -6.3 % |
| LIB-20261015-market-review | 12 | 12 | 2 | 0 | 0 | +0.7 % | -8.5 % |
| LIB-20261015-spot-entries | 2 | 2 | 0 | 0 | 0 | — | — |
| LIB-20261016-local-review | 1 | 1 | 1 | 0 | 0 | -1.4 % | -1.4 % |
| LIB-20261017-goodmood-market-review | 1 | 1 | 1 | 0 | 0 | -0.9 % | -1.4 % |

## Заклики

| id | Дата | Актив | Сторона | Зона | Ціль / стоп | Статус | Дійшло (днів) | Вхід | Результат | Макс. рух | Макс. просадка | «Одразу» | Дані |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AI-VVV | 2026-10-14 | VVV | long | 20 | 40 / — | чекає зону | — | — | — | — | — | +12.8 % | binanceusdm:VVV/USDT:USDT |
| AI-VIRTUAL | 2026-10-14 | VIRTUAL | long | 0.6–0.65 | — / — | у позиції | 2026-10-15 (1) | 0.65 | +0.1 % | +8.6 % | -4.6 % | -3.5 % | binanceusdm:VIRTUAL/USDT:USDT |
| AI-GRASS-1 | 2026-10-14 | GRASS | long | 0.58–0.6 | — / — | у позиції | 2026-10-15 (1) | 0.6 | +4.6 % | +21.4 % | -7.8 % | +0.8 % | binanceusdm:GRASS/USDT:USDT |
| AI-GRASS-2 | 2026-10-14 | GRASS | long | 0.4 | — / — | чекає зону | — | — | — | — | — | +0.8 % | binanceusdm:GRASS/USDT:USDT |
| AI-NIL | 2026-10-14 | NIL | long | 0.06 | 0.3 / — | чекає зону | — | — | — | — | — | -14.4 % | binanceusdm:NIL/USDT:USDT |
| AI-CHIP | 2026-10-14 | CHIP | long | 0.04 | — / — | у позиції | 2026-10-18 (4) | 0.04 | +10.8 % | +27.2 % | -3.1 % | -13.0 % | binanceusdm:CHIP/USDT:USDT |
| RWA-ONDO-1 | 2026-10-05 | ONDO | long | 0.4 | — / — | чекає зону | — | — | — | — | — | -14.2 % | binanceusdm:ONDO/USDT:USDT |
| RWA-ONDO-2 | 2026-10-05 | ONDO | long | 0.34–0.35 | — / — | чекає зону | — | — | — | — | — | -14.2 % | binanceusdm:ONDO/USDT:USDT |
| RWA-AAVE | 2026-10-05 | AAVE | long | 120 | — / — | чекає зону | — | — | — | — | — | -20.3 % | binanceusdm:AAVE/USDT:USDT |
| RWA-UNI | 2026-10-05 | UNI | long | 6.2–6.7 | — / — | чекає зону | — | — | — | — | — | -22.8 % | binanceusdm:UNI/USDT:USDT |
| RWA-ENA-1 | 2026-10-05 | ENA | long | 0.2 | — / — | у позиції | 2026-10-08 (3) | 0.2 | -0.9 % | +23.7 % | -12.8 % | -20.5 % | binanceusdm:ENA/USDT:USDT |
| RWA-ENA-2 | 2026-10-05 | ENA | long | 0.15 | — / — | чекає зону | — | — | — | — | — | -20.5 % | binanceusdm:ENA/USDT:USDT |
| RWA-SYRUP | 2026-10-05 | SYRUP | long | 0.2 | — / — | у позиції | 2026-10-15 (10) | 0.2 | -4.4 % | +7.8 % | -7.1 % | -23.5 % | binanceusdm:SYRUP/USDT:USDT |
| RWA-RAY | 2026-10-05 | RAY | long | 1.2 | — / — | чекає зону | — | — | — | — | — | -17.6 % | binanceusdm:RAYSOL/USDT:USDT |
| RWA-ZRO | 2026-10-05 | ZRO | long | 1.5–1.577 | — / — | чекає зону | — | — | — | — | — | -17.5 % | binanceusdm:ZRO/USDT:USDT |
| MR-BTC-LONG | 2026-10-15 | BTC | long | 80 000–82 000 | 85 400 / — | у позиції | 2026-10-16 (1) | 82 000 | -0.8 % | +3.5 % | -1.7 % | -1.0 % | binanceusdm:BTC/USDT:USDT |
| MR-BTC-SHORT | 2026-10-15 | BTC | short | 85 400–85 600 | — / — | чекає зону | — | — | — | — | — | +1.0 % | binanceusdm:BTC/USDT:USDT |
| MR-BTC-GLOBAL | 2026-10-15 | BTC | long | 75 000–76 000 | — / — | чекає зону | — | — | — | — | — | -1.0 % | binanceusdm:BTC/USDT:USDT |
| MR-HYPE | 2026-10-15 | HYPE | long | 75 | — / — | чекає зону | — | — | — | — | — | -5.3 % | binanceusdm:HYPE/USDT:USDT |
| MR-PUMP-1 | 2026-10-15 | PUMP | long | 0.0037–0.0038 | — / — | чекає зону | — | — | — | — | — | +0.2 % | binanceusdm:PUMP/USDT:USDT |
| MR-PUMP-2 | 2026-10-15 | PUMP | long | 0.0032 | — / — | чекає зону | — | — | — | — | — | +0.2 % | binanceusdm:PUMP/USDT:USDT |
| MR-ZRO-1 | 2026-10-15 | ZRO | long | 1.5 | 2.5 / — | чекає зону | — | — | — | — | — | -14.3 % | binanceusdm:ZRO/USDT:USDT |
| MR-ZRO-2 | 2026-10-15 | ZRO | long | 1.3 | 2.5 / — | чекає зону | — | — | — | — | — | -14.3 % | binanceusdm:ZRO/USDT:USDT |
| MR-AERO | 2026-10-15 | AERO | long | 0.75–0.77 | — / — | у позиції | 2026-10-18 (3) | 0.77 | +2.2 % | +16.1 % | -0.9 % | -16.0 % | binanceusdm:AERO/USDT:USDT |
| MR-ENA | 2026-10-15 | ENA | long | 0.146–0.158 | — / 0.14 | чекає зону | — | — | — | — | — | -3.3 % | binanceusdm:ENA/USDT:USDT |
| SPOT-NEAR | 2026-10-15 | NEAR | long | 3–3.16 | — / — | чекає зону | — | — | — | — | — | -7.2 % | binanceusdm:NEAR/USDT:USDT |
| SPOT-VIRTUAL | 2026-10-15 | VIRTUAL | long | 0.5 | — / — | чекає зону | — | — | — | — | — | -4.4 % | binanceusdm:VIRTUAL/USDT:USDT |
| DT-BTC-LAST | 2026-10-15 | BTC | long | 69 000–71 000 | — / 68 000 | чекає зону | — | — | — | — | — | -1.0 % | binanceusdm:BTC/USDT:USDT |
| DT-LIT | 2026-10-15 | LIT | long | 2.9–3.1 | — / — | у позиції | 2026-10-16 (1) | 3.026 | +11.9 % | +31.7 % | -6.6 % | +11.9 % | binanceusdm:LIT/USDT:USDT |
| DT-ASTER | 2026-10-15 | ASTER | long | 0.6 | — / — | чекає зону | — | — | — | — | — | -1.6 % | binanceusdm:ASTER/USDT:USDT |
| DT-ETH | 2026-10-15 | ETH | long | 2 357–2 370 | 2 540 / — | у позиції | 2026-10-17 (2) | 2 370 | +0.8 % | +6.6 % | -1.8 % | -2.2 % | binanceusdm:ETH/USDT:USDT |
| DT-AERO | 2026-10-15 | AERO | long | 0.8–0.85 | 1 / — | у позиції | 2026-10-17 (2) | 0.85 | -7.4 % | +5.2 % | -10.2 % | -16.0 % | binanceusdm:AERO/USDT:USDT |
| DT-CASHCAT | 2026-10-15 | CASHCAT | long | 0.1 | 1 / — | у позиції | 2026-10-17 (2) | 0.1 | +2.0 % | +23.9 % | -2.7 % | -18.7 % | bybit:CASHCAT/USDT:USDT |
| MR-VVV-1 | 2026-10-15 | VVV | long | 19.6–20.5 | — / — | чекає зону | — | — | — | — | — | +5.7 % | binanceusdm:VVV/USDT:USDT |
| MR-VVV-2 | 2026-10-15 | VVV | long | 15.5–16.6 | — / — | чекає зону | — | — | — | — | — | +5.7 % | binanceusdm:VVV/USDT:USDT |
| ETH-LOC | 2026-10-16 | ETH | long | 2 420–2 460 | 2 807 / 2 322 | у позиції | 2026-10-17 (1) | 2 422 | -1.4 % | +4.3 % | -3.9 % | -1.4 % | binanceusdm:ETH/USDT:USDT |
| GM-ETH-DEV | 2026-10-16 | ETH | long | 2 355–2 411 | 2 550 / — | у позиції | 2026-10-17 (1) | 2 411 | -0.9 % | +4.8 % | -3.5 % | -1.4 % | binanceusdm:ETH/USDT:USDT |
