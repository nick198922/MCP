# Звірка Coinalyze `.6` з Bybit і живий стан колектора `coinalyze_history` (27.11.2026)

Картка [IDEA-20261017-doubletop-to-ours](../ideas/IDEA-20261017-doubletop-to-ours.md), пункт 4c (зріз). Це **звірка даних**, не торгова гіпотеза: P&L немає, реєстрації не треба. Скрипт: `scripts/verify_coinalyze_bybit.py`, чисті функції: `src/tradedesk/analytics/coinalyze_recon.py` (тести `tests/test_coinalyze_recon.py`).

**Межі висновків:** лише BTC і ETH, лише денні бари. Funding — 1489 днів (30.10.2022–26.11.2026), лонг/шорт Bybit — лише 499 останніх днів (з 16.07.2025), OI Bybit — лише 401 день (з 22.10.2025), лонг/шорт Binance — лише 29 днів (◯). Інші монети, годинні бари й старіші дні лонг/шорт і OI біржею не перевірені.

## Допуски

Константи записано в скрипт і модуль до першого запуску порівняння; після побачених чисел їх не змінювали (і в правці за рев'ю теж). Окремим комітом до запуску їх не зафіксовано, тож порядок із git не видно.

| Що | Допуск |
|---|---|
| funding (десятковий дріб за інтервал) | \|ours − біржа\| ≤ 2e-8 (2 знаки точності бірж, 1e-8) |
| частка лонгів (0–1) | ≤ 1e-4 (1 знак точності Bybit `buyRatio`) |
| ratio лонг/шорт | відносно ≤ 1e-3 (другорядний рядок) |
| OI USD | відносно ≤ 1 % від «OI Bybit у монетах × close Bybit дня» |
| ліквідації: сума = частини | ≤ 1 USD + 1e-9 відносно |
| повторне читання бару | ≤ 1e-12 |
| вердикт | ✓, якщо частка збігів ≥ 99 %; ≈, якщо ≥ 90 %; ✗ нижче; ◯, якщо спільних точок < 30 |

Для funding і лонг/шорт перевірялося кілька **визначень** (яку виплату / який знімок відображає денний бар); у таблиці нижче — визначення, що збіглося, а всі інші варіанти наведено в сирому виводі (вони дають 0–52 % збігів). Маркери ✓ / ≈ / ✗ / ◯ виводить сам скрипт функцією `verdict()` (`result.json` → `verdicts`, кінець сирого виводу); таблиця їх копіює.

**Розрізняльність funding.** Дні, на яких правильне визначення відрізняється від **усіх** семи інших кандидатів (перша / остання / макс. / мін. / середня виплата доби, перша виплата D+1, остання виплата D−1) понад допуск: 298–365 з 1489 на ряд і поле (20–25 %). На решті днів ставки доби часто однакові (базова 0.0001), і будь-яке визначення збігається. Попарно розрізнення ширше: кожен хибний кандидат для `close` збігається на 16,5–50,6 % днів (макс. і мін. виплата доби — 43–51 %, решта — 16,5–23,8 %), тобто 49–83 % днів відкидають його.

## Метод

1. Таблиця `coinalyze_history` живої бази `/var/lib/tradedesk-mcp/tradedesk.sqlite`, відкрита лише на читання (`mode=ro`).
2. Історія бірж через провайдери (`build_server()`): виплати funding — `rt.exchanges.funding_rate_history_range` (ccxt, Bybit сторінками назад від `endTime`, Binance вперед), лонг/шорт — `rt.bybit.long_short_account_ratio` (1d і 1h, по 500 рядків) і `rt.binance_futures.long_short_account_ratio` (1d — 30 днів, Binance більше не зберігає; 1h — 500), OI Bybit — `rt.bybit.open_interest_range` 1d за 400 днів × close денних свічок Bybit (`rt.exchanges.ohlcv`). Кеш: `~/.cache/tradedesk-agents/coinalyze-bybit/`.
3. Через робочий MCP-сервіс (`scripts/mcp_call.py`, ключ є лише в сервісі; у клоні ключа немає, ключ не читався): `liquidations_history` для `binance`, `bybit` і `binance+bybit`, BTC і ETH, 30 днів; повторне читання `.A` funding через `perps_funding_history` source=coinalyze.

## 1. Живий стан колектора

Усі 12 рядів (BTC/ETH × `.A`/`.6` × `oi_usd` / `funding` / `long_short`): **1489 рядків, 30.10.2022–26.11.2026, 0 пропущених днів, 0 дублів, усі `ts` о 00:00 UTC, останній осілий день (26.11) є.** На всіх 1489 днях таблиці біржі мають рівно по 3 виплати на добу (Bybit і Binance, BTC і ETH; рядок `settlements per day` сирого виводу).

| Ряд | Порядок величини (медіана close; мін–макс) |
|---|---|
| `oi_usd` BTC `.A` / `.6` | 5,7e9 / 3,8e9 USD (1,6e9–1,2e10 / 6,6e8–8,8e9) — USD, не монети: `convert_to_usd` працює |
| `oi_usd` ETH `.A` / `.6` | 3,2e9 / 1,6e9 USD |
| `funding` усі | медіана 5,5e-5 – 6,6e-5 за інтервал (≈ 0,006 %), діапазон −5,5e-4…9,7e-4 — десятковий дріб, не відсотки |
| `long_short` | частка лонгів 0,30–0,90, сума часток = 1 (±2e-16), `ratio` = long/short у 100 % рядків |

## 2. Вердикти

| Ряд | Визначення, що збіглося | n | Період | Збіг | Вердикт |
|---|---|---|---|---|---|
| funding `.6` BTC, `close` | остання виплата, позначена в межах доби UTC (16:00 D) | 1489 | 30.10.2022–26.11.2026 | 100 % (макс. різниця 1e-19) | ✓ |
| funding `.6` BTC, `open` / `high` / `low` | перша / найбільша / найменша виплата доби | 1489 | те саме | 100 % / 100 % / 100 % | ✓ |
| funding `.6` ETH, `close`, `open`/`high`/`low` | те саме | 1489 | те саме | 100 % усі | ✓ |
| одиниці funding `.6` | Coinalyze шле відсотки, після /100 масштаб до Bybit = 1,000 | 1489 | | | ✓ |
| лонг/шорт `.6` BTC, частка лонгів і ratio | знімок рахунків Bybit `account-ratio` з тим самим `ts` (00:00 UTC дня бару) | 499 | 16.07.2025–26.11.2026 | 100 % | ✓ |
| лонг/шорт `.6` ETH | те саме | 499 | те саме | 100 % | ✓ |
| OI `.6` BTC (sanity) | OI Bybit, позначений на **наступну** 00:00 UTC × close Bybit дня | 401 | 22.10.2025–26.11.2026 | 100 % у межах 1 % (медіанна різниця 0, макс. 0,75 млн USD) | ✓ |
| OI `.6` ETH (sanity) | те саме | 401 | те саме | 100 % (медіанна різниця 0, макс. 1,47 млн USD) | ✓ |
| контроль `.A` BTC funding `close` | остання виплата доби (як і 18.10) | 1489 | 30.10.2022–26.11.2026 | 100 % | ✓ |
| контроль `.A` BTC funding `open` / `high` / `low` | перша / макс. / мін. виплата доби | 1489 | те саме | 1395 / 1473 / 1474 днів = 93,69 % / 98,93 % / 98,99 % | ≈ / ≈ / ≈ |
| контроль `.A` ETH funding `close` | те саме | 1489 | те саме | 100 % | ✓ |
| контроль `.A` ETH funding `open` / `high` / `low` | те саме | 1489 | те саме | 1387 / 1468 / 1458 днів = 93,15 % / 98,59 % / 97,92 % | ≈ / ≈ / ≈ |
| контроль `.A` лонг/шорт BTC і ETH | знімок `globalLongShortAccountRatio` з тим самим `ts` | 29 | 29.10–26.11.2026 | 100 % | ◯ (n < 30; Binance зберігає лише 30 днів) |
| `liquidations_history` BTC: binance+bybit = binance + bybit | закриті дні | 30 | 28.10–26.11.2026 | 100 % (різниця 0,0) | ✓ |
| `liquidations_history` ETH: те саме | закриті дні | 30 | те саме | 100 % (різниця 0,0) | ✓ |
| повторне читання `.A` funding (сервіс) проти таблиці, BTC і ETH | open/high/low/close | 30 | останні 30 днів | 100 % | ✓ |

### Чому ≈ у контролі Binance (розбір у скрипті, `result.json` → `funding.<символ>.<поле>.explain`)

На днях, де поле `.A` не дорівнює очікуваній виплаті доби, скрипт рахує, чому воно дорівнює натомість. Розрізняльні дні — ті, де остання виплата D−1 не дорівнює ні базовій ставці 0.0001, ні першій виплаті дня D: лише на них «перенесена ставка» і «базова ставка 0.0001» передбачають різні значення.

| Ряд, поле | Розбіжних днів з 1489 | = остання виплата D−1 | = 0.0001 | ні те, ні те | розрізняльних | з них = D−1 / = 0.0001 | поза діапазоном доби | за роками |
|---|---|---|---|---|---|---|---|---|
| BTC `.A` open | 94 (6,3 %) | 94 | 3 | 0 | 91 | 91 / 0 | 16 вище макс., 15 нижче мін. | 2025: 2, 2026: 92 |
| BTC `.A` high | 16 (1,1 %) | 16 | 0 | 0 | 16 | 16 / 0 | 16 вище макс. | 2026: 16 |
| BTC `.A` low | 15 (1,0 %) | 15 | 0 | 0 | 15 | 15 / 0 | 15 нижче мін. | 2025: 1, 2026: 14 |
| ETH `.A` open | 102 (6,9 %) | 102 | 1 | 0 | 101 | 101 / 0 | 21 вище макс., 31 нижче мін. | 2024: 3, 2025: 1, 2026: 98 |
| ETH `.A` high | 21 (1,4 %) | 21 | 0 | 0 | 21 | 21 / 0 | 21 вище макс. | 2026: 21 |
| ETH `.A` low | 31 (2,1 %) | 31 | 0 | 0 | 31 | 31 / 0 | 31 нижче мін. | 2024: 1, 2026: 30 |
| усі `.6`, усі `close` | 0 | | | | | | | |

Висновок у межах показаного: на **кожному** розбіжному дні значення дорівнює останній виплаті попереднього дня; на 91 з 94 (BTC open) і 101 з 102 (ETH open) днів ця виплата відрізняється і від 0.0001, і від першої виплати дня, і жодного разу бар не взяв 0.0001 замість неї. Отже, це перенесена ставка D−1, а не базова. На розбіжних днях `high` завжди вищий за максимум власних виплат доби (16/16, 21/21), а `low` нижчий за мінімум (15/15, 31/31), що узгоджується з `high = max(перенесена, власні)`, `low = min(…)`; кількості збігаються з числом днів, коли перенесений `open` лежить поза діапазоном доби (16 і 15, 21 і 31), але те, що це ті самі дні, скрипт окремо не перевіряв. Для Bybit `.6` у цьому вікні (BTC/ETH) розбіжних днів немає. Попереднє формулювання опису «high = largest settlement» виправлено (нижче).

Відкинуті визначення (усі ✗, див. сирий вивід): funding `close` = виплата 00:00 D+1 (ставка за останній інтервал доби) — 18–24 % (18,5–23,8 %); середнє виплат доби — 16,5–20,3 %; остання виплата D−1 — 16,7–21,8 %; лонг/шорт = знімок D+1 00:00 або D−1 — 0–1 %; годинні знімки 01:00, 12:00, 23:00 — 0–30 % (n = 20, ◯ за правилом, але різниця очевидна); OI = знімок того самого `ts` (початок дня) — 22–25 %.

## Що має знати користувач таблиці

- `funding.close` — ставка останньої виплати, **позначеної** в межах доби (для 8-год контракту — 16:00 UTC), а не виплата 00:00 D+1 за останні 8 годин доби (BTC/ETH, денні бари). Для Binance `open`/`high`/`low` інколи несуть ставку попереднього дня; брати `close`.
- `long_short` бару D — знімок о 00:00 UTC D, тобто **початок** дня, а `oi_usd.close` бару D — **кінець** дня (OI на 00:00 D+1). Між двома рядами одного `ts` — 24 години. Нотатку додано в [data-sources](../knowledge/data-sources.md).

## Зміни за результатами

- Формулювання (поведінка без змін): попередження `perps_funding_history` source=coinalyze (`tools/coinalyze.py`) і опис інструмента (`tools/perps.py`): «open, high and low may carry the previous day's last rate», `close` звірено на всіх днях 2022–2026 для BTC і ETH (інші бази не перевірені); тест у `tests/test_coinalyze_history.py` перевіряє нове формулювання і відсутність старого «high (= largest settlement)». Докстрінги `collector/coinalyze_history.py` і `analytics/coinalyze_series.py` оновлено (Bybit звірено, лонг/шорт — початок дня, OI — кінець дня, `convert_to_usd` підтверджено таблицею).
- Нічого не зберігається з хибною одиницею: таблиця й інструменти узгоджені з біржами.

## Застереження і що лишилось

- Bybit `account-ratio` наш провайдер бере лише останні 500 знімків (без `startTime`/`endTime`), тому лонг/шорт `.6` звірено за 499 днів, а не за всі 1489. Старіші дні — та сама функція й той самий парсер, але біржею не перевірені.
- OI — лише sanity за 401 день. Медіанна різниця з «OI Bybit × close Bybit» дорівнює 0, максимальна — 0,75 млн USD для BTC (≈ 0,02 % медіанного OI) і 1,47 млн для ETH (≈ 0,09 %), тож щонайменше на більшості днів Coinalyze множить саме на close Bybit; чи на всіх (25.10 бачили множення на close Binance), допуск 1 % не розрізняє.
- Ліквідації порівняно лише «сума = частини»; публічної історії ліквідацій Bybit, з якою можна звірити самі числа, немає.
- **Для розробника (не виправлено тут):** MCP мовчки ігнорує невідому назву аргументу. Виклик `perps_funding_history` з `symbol="ETH"` (параметр зветься `base`) повернув ряд `BTCUSDT_PERP.A` за замовчуванням без жодного попередження. На цьому спіткнувся й перший запуск цього скрипта (виправлено в скрипті). Критик підтвердив причину читанням коду SDK (наживо окремо не відтворено); пункт для розробника додано в `docs/BACKLOG.md` (найменше виправлення: `scripts/mcp_call.py` звіряє ключі з `inputSchema` і виходить з ненульовим кодом).
- Не входило в зріз і лишається в 4c: денні ліквідації в таблиці, топ-30 монет, OKX і Hyperliquid. Годинний funding / ліміт годинної історії (BACKLOG, пункт (в)) не перевірявся.

## Сирий вивід (скорочено; повний — `~/.cache/tradedesk-agents/coinalyze-bybit/result.txt` і `result.json`)

```
[19:13:09] 1. table /var/lib/tradedesk-mcp/tradedesk.sqlite (read-only), last settled day 2026-11-26
  BTCUSDT.6       funding    rows=1489 dup=0 miss=0 misal=0 2022-10-30..2026-11-26 last_settled=True close_median=6.233e-05 close_min=-0.0005293 close_max=0.0009208
  BTCUSDT.6       long_short rows=1489 dup=0 miss=0 misal=0 2022-10-30..2026-11-26 last_settled=True long_share_median=0.6103 ... ratio_consistent_share=1
  BTCUSDT.6       oi_usd     rows=1489 dup=0 miss=0 misal=0 2022-10-30..2026-11-26 last_settled=True close_median=3.76e+09 close_min=6.551e+08 close_max=8.789e+09
  BTCUSDT_PERP.A  oi_usd     rows=1489 ... close_median=5.723e+09
  ETHUSDT.6       oi_usd     rows=1489 ... close_median=1.649e+09
  ETHUSDT_PERP.A  oi_usd     rows=1489 ... close_median=3.214e+09
  (усі 12 рядів: rows=1489 dup=0 miss=0 misal=0 last_settled=True)
[19:13:20] funding bybit BTC: 4470 settlements   (ETH, Binance BTC/ETH: теж 4470)
  settlements per day (table days): {3: 1489}   (усі чотири ряди, запуск 19:28)

funding BTCUSDT.6 vs bybit settlements (walk complete=True, abs tol 2e-08)
  close best=last_in_day             n= 1489 match= 1.000 med|d|=        0 scale=      1 2022-10-30..2026-11-26 ✓
    close vs first_in_day            n= 1489 match= 0.216 med|d|=  2.7e-05 scale=      1 ✗
    close vs max_in_day              n= 1489 match= 0.506 ✗
    close vs min_in_day              n= 1489 match= 0.462 ✗
    close vs mean_in_day             n= 1489 match= 0.191 ✗
    close vs next_first              n= 1489 match= 0.222 ✗
  high best=max_in_day               n= 1489 match= 1.000 ✓
  low best=min_in_day                n= 1489 match= 1.000 ✓
  open best=first_in_day             n= 1489 match= 1.000 ✓
long/short BTCUSDT.6 vs bybit account ratio (share abs tol 0.0001, ratio rel 0.001)
  1d same_ts long_share              n=  499 match= 1.000 med|d|=        0 scale=      1 2025-07-16..2026-11-26 ✓
  1d same_ts ratio                   n=  499 match= 1.000 ✓
  1d next_day long_share             n=  500 match= 0.004 med|d|=   0.0156 ✗
  1d prev_day long_share             n=  498 match= 0.004 ✗
  1h 00:00 long_share                n=   20 match= 1.000 ◯
  1h 01:00 long_share                n=   20 match= 0.000 ◯
  1h 23:00 long_share                n=   21 match= 0.000 ◯
  1h next 00:00 long_share           n=   21 match= 0.000 ◯
  venue 1d vs venue 1h same ts       n=   21 match= 1.000 ◯
OI BTCUSDT.6 vs bybit coins x bybit close (rel tol 0.01)
  oi_same_ts                         n=  401 match= 0.254 med|d|= 8.52e+07 ✗
  oi_next_day                        n=  401 match= 1.000 med|d|=        0 2025-10-22..2026-11-26 ✓

funding ETHUSDT.6: close/high/low/open — n=1489, match 1.000 усі ✓ (next_first 0.238, mean 0.203)
long/short ETHUSDT.6: 1d same_ts long_share n=499 match=1.000 ✓; next_day 0.008, prev_day 0.008 ✗
OI ETHUSDT.6: oi_same_ts n=401 match=0.224 ✗; oi_next_day n=401 match=1.000 ✓

funding BTCUSDT_PERP.A vs binance settlements (walk complete=True, abs tol 2e-08)
  settlements per day (table days): {3: 1489}
  close best=last_in_day             n= 1489 match= 1.000 med|d|=        0 scale=      1 2022-10-30..2026-11-26 ✓
   close expected=last_in_day: informative days 357/1489; mismatch 0/1489 = prev_last 0, base 1e-4 0, neither 0; ...
  high best=max_in_day               n= 1489 match= 0.989 med|d|=        0 scale=      1 2022-10-30..2026-11-26 ≈
   high expected=max_in_day: informative days 365/1489; mismatch 16/1489 = prev_last 16, base 1e-4 0, neither 0; above day max 16, below day min 0; distinguishing 16 (= prev_last 16, = base 0); by year {2026: 16}
  low best=min_in_day                n= 1489 match= 0.990 med|d|=        0 scale=      1 2022-10-30..2026-11-26 ≈
   low expected=min_in_day: informative days 341/1489; mismatch 15/1489 = prev_last 15, base 1e-4 0, neither 0; above day max 0, below day min 15; distinguishing 15 (= prev_last 15, = base 0); by year {2025: 1, 2026: 14}
  open best=first_in_day             n= 1489 match= 0.937 med|d|=        0 scale=      1 2022-10-30..2026-11-26 ≈
   open expected=first_in_day: informative days 342/1489; mismatch 94/1489 = prev_last 94, base 1e-4 3, neither 0; above day max 16, below day min 15; distinguishing 91 (= prev_last 91, = base 0); by year {2025: 2, 2026: 92}
    close vs next_first              n= 1489 match= 0.196 ✗      close vs prev_last  n= 1488 match= 0.167 ✗
funding ETHUSDT_PERP.A vs binance settlements (walk complete=True, abs tol 2e-08)
  close best=last_in_day             n= 1489 match= 1.000 ✓  (informative days 364/1489, mismatch 0)
   high expected=max_in_day: informative days 357/1489; mismatch 21/1489 = prev_last 21, base 1e-4 0, neither 0; above day max 21, below day min 0; distinguishing 21 (= prev_last 21, = base 0); by year {2026: 21}
   low expected=min_in_day: informative days 363/1489; mismatch 31/1489 = prev_last 31, base 1e-4 0, neither 0; above day max 0, below day min 31; distinguishing 31 (= prev_last 31, = base 0); by year {2024: 1, 2026: 30}
   open expected=first_in_day: informative days 352/1489; mismatch 102/1489 = prev_last 102, base 1e-4 1, neither 0; above day max 21, below day min 31; distinguishing 101 (= prev_last 101, = base 0); by year {2024: 3, 2025: 1, 2026: 98}
    close vs next_first              n= 1489 match= 0.185 ✗      close vs prev_last  n= 1488 match= 0.177 ✗
long/short .A BTC / ETH: 1d same_ts long_share n=29 match=1.000 ◯; next_day n=30 match=0.000 ✗
(Bybit .6 BTC / ETH: mismatch 0/1489 для всіх чотирьох полів; informative days 298–360/1489)

liquidations BTC: binance ok=True rows=31 total=2262783121, bybit ok=True rows=31 total=856441316, both ok=True rows=31 total=3119224437
  both == binance + bybit (closed days) n=   30 match= 1.000 ✓ only_total=0 only_parts=0
  re-read .A funding open/high/low/close vs table n=30 match=1.000 ✓
liquidations ETH: binance rows=31 total=1751771878, bybit rows=31 total=383877069, both rows=31 total=2135648947
  both == binance + bybit (closed days) n=   30 match= 1.000 ✓ only_total=0 only_parts=0
  re-read .A funding open/high/low/close vs table n=30 match=1.000 ✓

verdict table (marks derived from the stats by analytics.coinalyze_recon.verdict; the report copies these)
  funding BTCUSDT.6 close/high/low/open      n= 1489 match= 1489 share= 1.00000 2022-10-30..2026-11-26 ✓ (кожне)
  long_short BTCUSDT.6 long_share / ratio    n=  499 match=  499 share= 1.00000 2025-07-16..2026-11-26 ✓
  oi_usd BTCUSDT.6 close                     n=  401 match=  401 share= 1.00000 2025-10-22..2026-11-26 ✓
  (ETHUSDT.6: ті самі n і ✓)
  funding BTCUSDT_PERP.A close               n= 1489 match= 1489 share= 1.00000 ✓
  funding BTCUSDT_PERP.A high                n= 1489 match= 1473 share= 0.98925 ≈
  funding BTCUSDT_PERP.A low                 n= 1489 match= 1474 share= 0.98993 ≈
  funding BTCUSDT_PERP.A open                n= 1489 match= 1395 share= 0.93687 ≈
  long_short BTCUSDT_PERP.A long_share/ratio n=   29 match=   29 share= 1.00000 2026-10-29..2026-11-26 ◯
  funding ETHUSDT_PERP.A close               n= 1489 match= 1489 share= 1.00000 ✓
  funding ETHUSDT_PERP.A high                n= 1489 match= 1468 share= 0.98590 ≈
  funding ETHUSDT_PERP.A low                 n= 1489 match= 1458 share= 0.97918 ≈
  funding ETHUSDT_PERP.A open                n= 1489 match= 1387 share= 0.93150 ≈
  long_short ETHUSDT_PERP.A long_share/ratio n=   29 match=   29 share= 1.00000 ◯
  liquidations BTC / ETH both = binance + bybit  n=   30 match=   30 ✓
  re-read .A funding BTC / ETH open/high/low/close  n= 30 match= 30 ✓ (кожне)
```

Розбір перенесеної ставки Binance спершу зробив одноразовий скрипт поза репозиторієм (`explain_binance_open.py` у кеші); за рев'ю його перенесено в `scripts/verify_coinalyze_bybit.py` (функція `mismatch_breakdown`, лічильники в `result.json`), і числа збіглися: 94 / 16 / 15 і 102 / 21 / 31 днів, усі = остання виплата D−1.

## Правки за рев'ю ([2026-11-27](../reviews/2026-11-27-coinalyze-bybit-check.md))

1. Твердження в описах інструментів, докстрінгах, базі знань, BACKLOG, картці й `docs/changes/` обмежено BTC/ETH, денними барами, вікнами й n (див. «Межі висновків»).
2. Маркер контролю `.A` BTC `low` (1474/1489 = 0,98993 < 0,99) виправлено з ✓ на ≈; маркери тепер виводить скрипт. Діапазон `next_first` — 18–24 % (18,5–23,8 %), середнього — 16,5–20,3 % (раніше «20–24 %» і «17–20 %»).
3. Розбір перенесеної ставки — у скрипті, з окремим лічильником розрізняльних днів (D−1 ≠ 0.0001 і ≠ першій виплаті дня): 91/91 і 101/101 = D−1, 0 = базова. Твердження «high поза максимумом доби» показано (16/16, 21/21) і лишено.
4. Неперевірені твердження: «`rows written 0` у `system_health` о 17:06» прибрано; «рівно 3 виплати на добу» тепер друкує скрипт для всіх чотирьох рядів.
5. Додано кандидата «остання виплата D−1» і число інформативних днів; тести на повторний `ts`, `within` з `rel_tol`, `settlements_per_day`, `informative_days`, `mismatch_breakdown`. Допуски й пороги вердикту не змінювались.

Примітка до запусків: перший повний запуск (19:13) мав помилку в скрипті: повторне читання ETH викликало `perps_funding_history` з `symbol` замість `base` і отримало ряд BTC (✗ 3–13 %). Виправлено на `base`, другий запуск (19:14, дані бірж із кешу) дав 100 %. Решта чисел обох запусків однакова (суми ліквідацій відрізняються на кілька тисяч USD, бо сервіс за хвилину перечитав поточні дані Coinalyze; на звірку «сума = частини» це не впливає). Після правок за рев'ю — запуски 19:28 і 19:32 (дані бірж із того самого кешу, ліквідації заново з сервісу). Числа звірки ті самі, додано розбір розбіжних днів і таблицю вердиктів. Нинішні `result.json` / `result.txt` — від запуску 19:32.

Відтворити: `uv run python scripts/verify_coinalyze_bybit.py --brief --out ~/.cache/tradedesk-agents/coinalyze-bybit/result.json` (`--refresh` — заново з бірж).
