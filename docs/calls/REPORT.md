# Трекер закликів: звіт

Звіт на 2026-10-25 (`scripts/calls_check.py`; денні свічки перпів, лише завершені дні UTC). Заклики: [`calls.csv`](calls.csv); правила оцінки: [`src/tradedesk/analytics/calls.py`](../../src/tradedesk/analytics/calls.py).

Як читати:
- «Результат» — дохідність угоди від входу в зоні до цілі, стопу або останнього закриття, у напрямку заклику.
- «Одразу» — якби діяли в день після заклику, не чекаючи зони. Заклик чогось вартий, якщо результат стабільно кращий.

## Підсумок за джерелами

| Джерело | Закликів | З даними | Дійшло до зони | Ціль | Стоп | Сер. результат | Сер. «одразу» |
|---|---|---|---|---|---|---|---|
| LIB-20261014-ai-narrative | 6 | 6 | 3 | 0 | 0 | +7.8 % | -2.9 % |
| LIB-20261014-rwa-narrative | 9 | 9 | 4 | 0 | 0 | -2.1 % | -23.2 % |
| LIB-20261015-doubletop-stream | 6 | 6 | 4 | 0 | 0 | -4.1 % | -11.6 % |
| LIB-20261015-market-review | 12 | 12 | 3 | 0 | 0 | -2.8 % | -11.1 % |
| LIB-20261015-spot-entries | 2 | 2 | 0 | 0 | 0 | — | — |
| LIB-20261016-local-review | 1 | 1 | 1 | 0 | 1 | -4.1 % | -2.7 % |
| LIB-20261017-goodmood-market-review | 1 | 1 | 1 | 0 | 0 | -2.2 % | -2.7 % |
| LIB-20261024-nonquant-channel | 5 | 5 | 4 | 0 | 0 | +24.8 % | +20.5 % |

## Заклики

| id | Дата | Актив | Сторона | Зона | Ціль / стоп | Статус | Дійшло (днів) | Вхід | Результат | Макс. рух | Макс. просадка | «Одразу» | Дані |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AI-VVV | 2026-10-14 | VVV | long | 20 | 40 / — | чекає зону | — | — | — | — | — | +4.1 % | binanceusdm:VVV/USDT:USDT |
| AI-VIRTUAL | 2026-10-14 | VIRTUAL | long | 0.6–0.65 | — / — | у позиції | 2026-10-15 (1) | 0.65 | -3.3 % | +8.6 % | -6.9 % | -6.8 % | binanceusdm:VIRTUAL/USDT:USDT |
| AI-GRASS-1 | 2026-10-14 | GRASS | long | 0.58–0.6 | — / — | у позиції | 2026-10-15 (1) | 0.6 | +12.6 % | +21.4 % | -7.8 % | +8.6 % | binanceusdm:GRASS/USDT:USDT |
| AI-GRASS-2 | 2026-10-14 | GRASS | long | 0.4 | — / — | чекає зону | — | — | — | — | — | +8.6 % | binanceusdm:GRASS/USDT:USDT |
| AI-NIL | 2026-10-14 | NIL | long | 0.06 | 0.3 / — | чекає зону | — | — | — | — | — | -3.3 % | binanceusdm:NIL/USDT:USDT |
| AI-CHIP | 2026-10-14 | CHIP | long | 0.04 | — / — | у позиції | 2026-10-18 (4) | 0.04 | +14.1 % | +27.2 % | -3.1 % | -10.5 % | binanceusdm:CHIP/USDT:USDT |
| RWA-ONDO-1 | 2026-10-05 | ONDO | long | 0.4 | — / — | у позиції | 2026-10-24 (19) | 0.4 | +2.4 % | — | -0.9 % | -18.0 % | binanceusdm:ONDO/USDT:USDT |
| RWA-ONDO-2 | 2026-10-05 | ONDO | long | 0.34–0.35 | — / — | чекає зону | — | — | — | — | — | -18.0 % | binanceusdm:ONDO/USDT:USDT |
| RWA-AAVE | 2026-10-05 | AAVE | long | 120 | — / — | чекає зону | — | — | — | — | — | -25.1 % | binanceusdm:AAVE/USDT:USDT |
| RWA-UNI | 2026-10-05 | UNI | long | 6.2–6.7 | — / — | у позиції | 2026-10-24 (19) | 6.7 | +2.4 % | — | -3.0 % | -24.4 % | binanceusdm:UNI/USDT:USDT |
| RWA-ENA-1 | 2026-10-05 | ENA | long | 0.2 | — / — | у позиції | 2026-10-08 (3) | 0.2 | -2.4 % | +23.7 % | -12.8 % | -21.6 % | binanceusdm:ENA/USDT:USDT |
| RWA-ENA-2 | 2026-10-05 | ENA | long | 0.15 | — / — | чекає зону | — | — | — | — | — | -21.6 % | binanceusdm:ENA/USDT:USDT |
| RWA-SYRUP | 2026-10-05 | SYRUP | long | 0.2 | — / — | у позиції | 2026-10-15 (10) | 0.2 | -11.0 % | +7.8 % | -13.9 % | -28.8 % | binanceusdm:SYRUP/USDT:USDT |
| RWA-RAY | 2026-10-05 | RAY | long | 1.2 | — / — | чекає зону | — | — | — | — | — | -17.5 % | binanceusdm:RAYSOL/USDT:USDT |
| RWA-ZRO | 2026-10-05 | ZRO | long | 1.5–1.577 | — / — | чекає зону | — | — | — | — | — | -18.9 % | binanceusdm:ZRO/USDT:USDT |
| MR-BTC-LONG | 2026-10-15 | BTC | long | 80 000–82 000 | 85 400 / — | у позиції | 2026-10-16 (1) | 82 000 | -1.4 % | +3.5 % | -3.4 % | -1.6 % | binanceusdm:BTC/USDT:USDT |
| MR-BTC-SHORT | 2026-10-15 | BTC | short | 85 400–85 600 | — / — | чекає зону | — | — | — | — | — | +1.6 % | binanceusdm:BTC/USDT:USDT |
| MR-BTC-GLOBAL | 2026-10-15 | BTC | long | 75 000–76 000 | — / — | чекає зону | — | — | — | — | — | -1.6 % | binanceusdm:BTC/USDT:USDT |
| MR-HYPE | 2026-10-15 | HYPE | long | 75 | — / — | у позиції | 2026-10-24 (9) | 75 | +2.2 % | — | -0.8 % | -6.3 % | binanceusdm:HYPE/USDT:USDT |
| MR-PUMP-1 | 2026-10-15 | PUMP | long | 0.0037–0.0038 | — / — | чекає зону | — | — | — | — | — | -5.0 % | binanceusdm:PUMP/USDT:USDT |
| MR-PUMP-2 | 2026-10-15 | PUMP | long | 0.0032 | — / — | чекає зону | — | — | — | — | — | -5.0 % | binanceusdm:PUMP/USDT:USDT |
| MR-ZRO-1 | 2026-10-15 | ZRO | long | 1.5 | 2.5 / — | чекає зону | — | — | — | — | — | -15.7 % | binanceusdm:ZRO/USDT:USDT |
| MR-ZRO-2 | 2026-10-15 | ZRO | long | 1.3 | 2.5 / — | чекає зону | — | — | — | — | — | -15.7 % | binanceusdm:ZRO/USDT:USDT |
| MR-AERO | 2026-10-15 | AERO | long | 0.75–0.77 | — / — | у позиції | 2026-10-18 (3) | 0.77 | -9.1 % | +16.1 % | -11.8 % | -25.3 % | binanceusdm:AERO/USDT:USDT |
| MR-ENA | 2026-10-15 | ENA | long | 0.146–0.158 | — / 0.14 | чекає зону | — | — | — | — | — | -4.7 % | binanceusdm:ENA/USDT:USDT |
| SPOT-NEAR | 2026-10-15 | NEAR | long | 3–3.16 | — / — | чекає зону | — | — | — | — | — | -14.5 % | binanceusdm:NEAR/USDT:USDT |
| SPOT-VIRTUAL | 2026-10-15 | VIRTUAL | long | 0.5 | — / — | чекає зону | — | — | — | — | — | -7.7 % | binanceusdm:VIRTUAL/USDT:USDT |
| DT-BTC-LAST | 2026-10-15 | BTC | long | 69 000–71 000 | — / 68 000 | чекає зону | — | — | — | — | — | -1.6 % | binanceusdm:BTC/USDT:USDT |
| DT-LIT | 2026-10-15 | LIT | long | 2.9–3.1 | — / — | у позиції | 2026-10-16 (1) | 3.026 | +6.6 % | +31.7 % | -6.6 % | +6.6 % | binanceusdm:LIT/USDT:USDT |
| DT-ASTER | 2026-10-15 | ASTER | long | 0.6 | — / — | чекає зону | — | — | — | — | — | +0.7 % | binanceusdm:ASTER/USDT:USDT |
| DT-ETH | 2026-10-15 | ETH | long | 2 357–2 370 | 2 540 / — | у позиції | 2026-10-17 (2) | 2 370 | -0.5 % | +6.6 % | -3.7 % | -3.4 % | binanceusdm:ETH/USDT:USDT |
| DT-AERO | 2026-10-15 | AERO | long | 0.8–0.85 | 1 / — | у позиції | 2026-10-17 (2) | 0.85 | -17.7 % | +5.2 % | -20.1 % | -25.3 % | binanceusdm:AERO/USDT:USDT |
| DT-CASHCAT | 2026-10-15 | CASHCAT | long | 0.1 | 1 / — | у позиції | 2026-10-17 (2) | 0.1 | -5.0 % | +23.9 % | -11.9 % | -24.3 % | bybit:CASHCAT/USDT:USDT |
| MR-VVV-1 | 2026-10-15 | VVV | long | 19.6–20.5 | — / — | чекає зону | — | — | — | — | — | -2.5 % | binanceusdm:VVV/USDT:USDT |
| MR-VVV-2 | 2026-10-15 | VVV | long | 15.5–16.6 | — / — | чекає зону | — | — | — | — | — | -2.5 % | binanceusdm:VVV/USDT:USDT |
| ETH-LOC | 2026-10-16 | ETH | long | 2 420–2 460 | 2 807 / 2 322 | стоп ✗ | 2026-10-17 (1) | 2 422 | -4.1 % | +4.3 % | -5.8 % | -2.7 % | binanceusdm:ETH/USDT:USDT |
| GM-ETH-DEV | 2026-10-16 | ETH | long | 2 355–2 411 | 2 550 / — | у позиції | 2026-10-17 (1) | 2 411 | -2.2 % | +4.8 % | -5.4 % | -2.7 % | binanceusdm:ETH/USDT:USDT |
| NQ-BTC-0702 | 2026-07-02 | BTC | long | 61 560 | 66 000 / — | закрито за часом | 2026-07-03 (1) | 61 540 | +3.7 % | +6.6 % | -0.5 % | +3.7 % | binanceusdm:BTC/USDT:USDT |
| NQ-HYPE-0723 | 2026-07-23 | HYPE | long | 50–52 | — / — | закрито за часом | 2026-07-31 (8) | 52 | +55.1 % | +88.6 % | -1.7 % | +40.3 % | binanceusdm:HYPE/USDT:USDT |
| NQ-BTC-0824 | 2026-08-24 | BTC | long | 72 000–74 000 | — / — | чекає зону | — | — | — | — | — | +2.4 % | binanceusdm:BTC/USDT:USDT |
| NQ-BTC-0902 | 2026-09-02 | BTC | long | 75 500 | — / — | у позиції | 2026-09-15 (13) | 75 500 | +7.1 % | +15.7 % | -0.8 % | +4.6 % | binanceusdm:BTC/USDT:USDT |
| NQ-AERO-0915 | 2026-09-15 | AERO | long | 0.5254 | 1 / — | у позиції | 2026-09-16 (1) | 0.5252 | +33.3 % | +89.0 % | -1.6 % | +33.3 % | binanceusdm:AERO/USDT:USDT |
