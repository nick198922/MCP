# Звірка з DOUBLETOP, крок 1b: ETH, альт, Bybit і суфікс `.A`

Дата: 2026-10-18. Картка: [IDEA-20261017-doubletop-to-ours](../ideas/IDEA-20261017-doubletop-to-ours.md). Попередній крок: [1a](2026-10-18-doubletop-parity.md) і [рев'ю критика](2026-10-18-doubletop-parity-critic.md).

## Що зроблено і чого не вдалося

- **Наш бік знято повністю** для BTC, ETH і SOL (альт): ohlcv Binance 1d, Binance `openInterestHist` 1d, `globalLongShortAccountRatio` 1d, виплати funding Binance USDT-M (18 рядків, ~6 діб), Bybit `open-interest` 1d і `account-ratio` 1d. Знімав `scripts/doubletop_parity_1b.py` (мережа лише через провайдери `Runtime`), файли — `tests/fixtures/doubletop/ours__*_2026-10-18.json` (дата й джерело в `_source`).
- **Конектор DOUBLETOP розробнику не був відкритий**, тому розділ «Що перевірено на нашому боці» — без DT. Звірку ETH з DT (OI, funding, long/short) пізніше зробив координатор: розділ «Доповнення координатора». Сирі відповіді DT **не збережено** (завеликі), тож цифри DT відтворити з репозиторію не можна — лише повторити запит.
- Закрито: ETH OI/funding/long-short. Відкрито: ETH ohlcv, SOL (крім long/short), Bybit-гілки (немає коду біржі Bybit у Coinalyze без `/exchanges`).

## Що перевірено на нашому боці (без DT)

| Перевірка | Результат |
|---|---|
| Узгодженість одиниць у ETH/SOL з BTC | Binance OI `sumOpenInterest` у базових монетах (ETH ≈ 2.27 млн, SOL ≈ 8.5-9.2 млн); `longAccount + shortAccount = 1`, `longShortRatio = long/short` (ETH 0.7267 / 2.659). Усе в тих самих одиницях, що BTC, тож правила переходу до Coinalyze (l, s у % = ×100; OI у базовій монеті) переносяться без змін. |
| Funding Binance (ETH/SOL/BTC) | Десяткові дроби за виплату (ETH 4.045e-05 тощо), мітки з джитером 0–3 мс, інтервал 8 год; той самий формат, що для BTC у 1a. |
| Bybit OI, поле | Відповідь Bybit має `openInterest` і `singleOpenInterest` (рівно вдвічі менше). Наш `perps_oi_history` бере `openInterest`. Перевірено проти тікера Bybit (`openInterest` 57 394 BTC, ccxt `fetch_open_interest` 58 167) — наше поле правильне (повний обсяг), `singleOpenInterest` не брати. |
| Bybit OI, порядок | Bybit віддає від нових до старих, ми сортуємо за `ts_ms`. Мітка 1d Bybit — 00:00 UTC, як у Binance. |
| Bybit long/short | `buyRatio + sellRatio = 1`, рахунки (не нотіонал), як у Binance. BTC 0.6337 проти Binance 0.56 — інша когорта, розбіжність нормальна. |

## Суфікс `.A` у Coinalyze: відповідь

**`.A` = Binance, а не агрегат бірж.** Тому наш `liquidations_history` віддавав ліквідації **лише Binance**, хоча опис казав «aggregated across exchanges». Це було оманливо.

Три незалежні докази (прямого виклику `/exchanges` немає: потрібен ключ, у цього запуску його нема, відповідь 401):
1. **Каталог самого DoubleTop** (`docs/reference/doubletop-catalog.md`, рядок про `symbols`): «суфікс — код біржі: `.A` = Binance». Це їхня власна схема інструмента.
2. **OI в числах** (зразок DT `BTCUSDT_PERP.A`, 1d): `o` 92 493.4 BTC проти Binance `sumOpenInterest` 92 500.2 (різниця 0.007 %). Bybit BTCUSDT на ті ж доби: 56-61 тис. BTC. Якщо б `.A` був сумою бірж, він був би не менший за Binance + Bybit ≈ 150 тис. BTC.
3. **Funding і long/short у 1a:** збіг із Binance до 6-го і 4-го знака на всіх днях (агрегат так точно не збігся б).

Що виправлено (код, англійською; тест є):
- `tools/coinalyze.py`: опис і `warnings` кажуть «Binance only», «не читати як ліквідації всього ринку»; тест `test_liquidations_history_happy_and_key_not_leaked` перевіряє, що застереження є.
- `analytics/liquidations_agg.py` (`aggregated_symbol`, назву функції лишено, щоб не чіпати виклики), `providers/coinalyze.py`: докстрінги.
- `README.md`, `docs/PARITY.md`: «лише Binance».

**Наслідок для картки:** крок 5 («каскади із сумарними ліквідаціями всіх бірж») не можна будувати на `liquidations_history` як на агрегаті. Для справжнього агрегату треба сумувати символи кількох бірж (потрібні коди бірж із `/exchanges`) або використати власний збирач `liquidations_summary` (WebSocket, кілька бірж). У `docs/BACKLOG.md` пункт про `.A` оновлено: лишилось живе `/exchanges` власником і коди інших бірж.

## Доповнення координатора: ETH проти DOUBLETOP (Coinalyze `ETHUSDT_PERP.A`, daily)

Конектор DOUBLETOP був відкритий координатору (не розробнику), тому ETH звірено тут. Сирі відповіді (~1 рік історії, дуже великі) у фікстури не збережено; порівняно останні дні.

| Метрика | Результат |
|---|---|
| Long/short | **Збіг точний**: `t=1792195200` DT `r 2.917, l 74.47` = наш `longShortRatio 2.9170, longAccount 0.7447`; так само 10-16 (2.639 / 72.52) і 10-18 (3.08 / 75.49). Множник ×100 для `l`, `s` підтверджено. |
| Funding | **Збіг**: DT `c` ×1e-2 = наш дріб. Денний `c` за 10-17 = 0.006616 (%) = наша виплата 16:00 UTC 6.616e-05; за 10-18 = 0.004045 = 4.045e-05 (08:00). Тобто `c` — остання виплата доби, одиниця — відсотки. |
| OI | **Збіг із зсувом на добу**: DT `t=1792195200` `c=2 266 347` ≈ наш Binance `timestamp=1792281600` `2 266 568` (0.01 %); DT `t=1792108800` `c=2 250 957` ≈ наш `1792195200` `2 250 394` (0.025 %). Мітка Binance `openInterestHist` — кінець періоду, Coinalyze `t` — початок. При накладанні рядів за `t` OI зсувати на 1 добу (LSR зсуву не має). |
| SOL | Лише long/short з DT (`SOLUSDT_PERP.A`) отримано; OI/funding SOL відповідь обрізала. Порівняння з нашим SOL не робилось. |
| Bybit | Не робилось (код біржі Bybit у Coinalyze невідомий без `/exchanges`). |

## Доповнення 2: ETH ohlcv і SOL OI/funding проти DOUBLETOP (координатор, 2026-10-18, пізніше)

Сирі відповіді DT збережено (4 доби): `tests/fixtures/doubletop/ccxt__get-ohlcv_binance_ETHUSDT_1d_*`, `coinalyze__open_interest_history_SOLUSDT_PERP.A_daily_*`, `coinalyze__funding_rate_history_SOLUSDT_PERP.A_daily_*`. DT-бік ETH OI/funding/LSR (доповнення 1) і SOL LSR як фікстури не збережено: відтворити можна лише повторним запитом.

| Метрика | Результат |
|---|---|
| ETH ohlcv Binance 1d | **Збіг точний** на трьох закритих свічках (open/high/low/close/volume до останнього знака). Остання (незакрита) свічка різниться: знято в різний час. |
| SOL funding | **Збіг точний.** DT `c` (у %) ×1e-2 = наша виплата: 10-15 `c` 0.003927 = 3.927e-05 (16:00), 10-16 −0.001557 = −1.557e-05, 10-17 −0.001862 = −1.862e-05, 10-18 0.004105 = 4.105e-05 (08:00). `h` дня = максимум виплат доби (10-15: 9.741e-05 о 00:00). `o` = виплата 16:00 попередньої доби (10-15 `o` −0.000183 = −1.83e-06 від 10-14 16:00), тож денний бар охоплює виплати з 16:00 попередньої доби. |
| SOL OI | **Збіг лише частково.** Збіг на двох мітках: `t=1792022400` DT `o` 8 964 606 проти Binance 8 961 861 (0.03 %); `t=1792281600` DT `o` 8 830 498 проти Binance 8 831 039 (0.006 %). На `t=1792108800` і `1792195200` DT `o` 8.82 і 8.93 млн проти Binance 9.09 і 9.20 млн (розбіжність ≈ 3 %). Причина не встановлена (інший момент знімка в добі чи інша вибірка Coinalyze); висновок `.A` = Binance (порядок величини, funding, LSR) не змінюється, але **для OI точність накладання ±3 %, не ±0.25 %**. Для BTC (доповнення 1) розбіжності були ≤ 0.24 %. |

## Що лишається у 1b

0. Закрито доповненням 2: ETH ohlcv, SOL OI/funding. Лишається нижче лише Bybit-гілка (код біржі з `/exchanges`, ключ у власника), вікно Hyperliquid з ненульовою `premium` і Polymarket (наш бік).

1. (ETH OI/funding/long-short — зроблено, див. доповнення; лишається ETH ohlcv, SOL OI/funding і збережені обрізані зразки DT.) Зразки DOUBLETOP для ETH і SOL (ohlcv, OI, funding, long/short, `coinalyze__*` символи `ETHUSDT_PERP.A`, `SOLUSDT_PERP.A`, 1d, те саме вікно 4-5 діб) і порівняння з `ours__*`. Очікування (гіпотеза, не результат): множник ×100 для funding та l/s, OI в монетах, мітка `o` ближча до Binance на початок доби, ohlcv збігається до останньої незакритої свічки.
2. Бажано один зразок DT для Bybit-гілки (символ Bybit у Coinalyze, код біржі з `/exchanges`).
3. Для Hyperliquid взяти вікно, де `premium` не нульова (у наших `ours__hyperliquid_funding_history_BTC_*` вона змінна).
4. Збереження Polymarket-відповіді нашого боку (досі не збережено).

Картку не відмічено: пункт 1b закрито частково (відповідь про `.A` отримано, DT-зразки ETH/SOL/Bybit — ні).
