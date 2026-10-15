# Шорт нових USDT-перпів Binance у перші 2–4 тижні: стадія 0 (потужність) — 2026-10-15

Картка: `docs/ideas/IDEA-20261015-new-perp-listing-drift.md`. Попередню реєстрацію записано в картці й проіндексовано (`git add`) до будь-якого обчислення доходів. Тріаж критика умовний (8 умов). Скрипт: `scripts/verify_listing_drift.py` (стадії `meta`, `fetch0`, `age`, `stage0`). Аналітика: `src/tradedesk/analytics/listing_drift.py`, тести: `tests/test_listing_drift.py`. Кеш поза git: `~/.cache/tradedesk-agents/listing_drift/`.

## Підсумок

**Вердикт: ◯ `inconclusive`. Ворота потужності не пройдено, тож середні доходи не рахувались.** Подій достатньо: 541 лістинг у 54 місяцях 2022-01 … 2026-09. Але розкид 14-денного результату шорта величезний (SD 42.9 п.п.), і з кластеризацією за місяцями найменший виявний ефект становить **7.25 п.п.** (через ICC 5.83 п.п.) проти зареєстрованого порогу **5 п.п.** Ефект +5 п.п., який стверджує картка, на цих даних не можна ні підтвердити, ні відкинути з потужністю 80 %. Через це вердикт `inconclusive`, а не `rejected` (умова 1 критика). Висновку «ефекту немає» тут немає, як і висновку «ефект є».

| Пункт | Поріг | Результат | Вердикт |
|---|---|---|---|
| Подій (один символ на актив, t0 у 2022-01-01 … 2026-09-15) | ≥ 150 | 541 | ✓ |
| Різних місяців | ≥ 24 | 54 | ✓ |
| SD метрики стадії 0 (H = 14, ліквідація 1x, без funding) | — | 42.93 п.п. | — |
| ICC за місяцем t0 | — | +0.018 (deff 1.27, n_eff 425) | — |
| MDE_icc = 2.8·SD·√(deff/n) | — | 5.83 п.п. | ✗ |
| MDE_cr = 2.8·SE(CR1, місяці) | — | 7.25 п.п. | ✗ |
| **Ворота: max(MDE_icc, MDE_cr) ≤ 5 п.п.** | ≤ 5 п.п. | **7.25 п.п.** | **✗ → `inconclusive`** |
| H1–H4, placebo, кошик, хвіст | — | не рахувались (так зареєстровано) | ◯ |

Навіть за м'якшим порогом картки до тріажу (6 п.п.) ворота не проходять за зареєстрованим правилом max: MDE_icc 5.83 ≤ 6, але MDE_cr 7.25 > 6. Funding на стадії 0 не враховувався. Він лише додав би дисперсії, тож повний H1 мав би MDE не менший.

Скільки подій потрібно: за нинішніх SD і кластеризації MDE ≤ 5 п.п. вимагає ≈ 736 подій (за ICC) або ≈ 1137 (за CR1). Зараз їх 541. За темпом 2025 року (≈ 240 лістингів на рік) це ще 1–2.5 року даних.

## Метод (як виконано умови критика)

**Джерело і survivorship (умова 7).** Ціни взято з архіву `data.binance.vision`. Каталог USDT-M свічок архіву містить 1056 символів, разом із повністю прибраними з `exchangeInfo`. Провайдер `binance_vision` перенесено з незлитої гілки liquidation-flush і доповнено місячними архівами funding. Мережа йде лише через `core.http.Http` і `rt.exchanges`. З 900 символів `…USDT` без `_` відкинуто 212 TradFi (`contractType = TRADIFI_PERPETUAL`), 3 індекси (`underlyingType = INDEX`), а також стейбли, обгорнуті токени, золото і `…SETTLED`. Лишилось 680 придатних символів, із них 32 повністю прибрані з `exchangeInfo`. Серед 541 події сьогодні торгуються 427, у статусі SETTLING 104, повністю прибрано 10 (1000BTTC, AERGO, ANC, BDXN, COCOS, FRONT, GAL, MBL, MDT, RNDR). Їх знайдено лише в архіві, тому для Binance зсув виживання закрито напряму, без припущень. За роками: 2022 — 24, 2023 — 98, 2024 — 131, 2025 — 238, 2026 — 50 подій. 2022 рік тонкий, як і передбачав критик (≈ 19 за метаданими, в архіві 24).

**Подія і вхід (умова 4).** t0 — відкриття першого 1h-бару з ненульовим обсягом в архіві. Вхід — перший денний open, що настає не раніше ніж через 24 год після t0. Фактично між t0 і входом минає від 29 до 47 год. Найчастіше перша угода припадає на 12:00 UTC (111 з 541). Правило входу читає лише час t0; тест `test_trade_does_not_depend_on_bars_after_exit` перевіряє, що зміна барів після виходу не змінює угоду. 1000x-тикери нормовано через `normalize_asset`. Один актив мав два символи: `BOBUSDT` (2025-11) після `1000000BOBUSDT` (2025-06), тож подією став лише перший.

**Перелістинги й ребрендинги (умова 7).** У 11 символів `onboardDate` розходиться з першою угодою понад 2 доби. У 6 з них символ торгувався задовго до нинішнього `onboardDate`, тобто це перелістинг: AIA, BNX, CVX, MAVIA, PUMP, SLP. Подією в усіх випадках є перша поява символу. Для PUMP це квітень 2025, а pump.fun з'явився в липні, тож лістинг pump.fun подією не став: це один пропущений лістинг, а не помилка ціни. У решти 5 (EDU, HIGH, HOOK, LQTY, MINA) розбіжність 2–4 доби: `onboardDate` старший за першу угоду. Медіана «перша угода − `onboardDate`» становить −0.4 год, тобто перший бар відкривається на початку години лістингу.

**Вік токена (умова 3).** Для кожної події зібрано свідчення ринку з п'яти джерел:
- спот Binance: архів, пари USDT, BUSD і BTC, точний день першої свічки;
- спот і linear-перп Bybit: сирі свічки;
- OKX spot і swap: `listTime`;
- перп Hyperliquid: денні свічки.

Свічки Bybit і HL з рівнем ціни поза [0.5; 2] від Binance на дату входу вважались іншим токеном з тим самим тікером. Так відкинуто 10 ніг Bybit і 1 Binance. Результат: 331 подія — новий токен (ринку ≤ 30 днів), 81 — 31–365 днів, 129 — понад рік. На іншому перпі (Bybit, OKX, HL) раніше ніж за добу до Binance торгувалися 212 з 541. Структура змінилась: у 2023 році 59 з 98 подій були «старими» токенами (> 1 року), а в 2025 році 196 з 238 — новими. Головною вибіркою наперед зафіксовано всі події, «новий токен» — вторинна з Holm. Для неї MDE ще гірший (8.1 / 10.3 п.п. при n = 331).

**Метрика стадії 0.** P&L шорта на номінал з ліквідацією 1x: якщо денний high ≥ вхід × 1.975, P&L = −100 %. До нього додається дохід перпа BTCUSDT за те саме вікно і віднімається 0.25 % витрат. Funding на стадії 0 не завантажувався. ICC — однофакторна ANOVA за календарним місяцем t0, deff = 1 + (Σm²/n − 1)·ICC. Друге оцінювання MDE — через кластерно-робастну SE (CR1) за місяцями. Ворота брали більше з двох, як зареєстровано.

**Перевірки даних (без перегляду середніх):**
- закриття BTCUSDT-перпа 2024-12-31 = 93 548.9, як у звіті про анлоки ✓;
- ARBUSDT: перша угода 2023-03-23 15:00 UTC, `onboardDate` того ж дня о 07:00. Це день аірдропу й початку торгів ARB ✓;
- денний close першого повного дня збігається з останнім 1h-close: 518 з 518 ✓;
- пропуск денного бару у вікні H = 14 лише в однієї події (IMX, 1 день);
- рухи ×3 за добу у вікні є у трьох подіях: PNUT 2024-11-13 ×3.8, COAI 2025-10-06 ×6.2, AIA 2025-10-02 ×3.2. Це відомі реальні сквізи (PNUT після лістингу на споті Binance, COAI і AIA у жовтні 2025), а не помилки архіву. Саме такі хвости й роблять SD великою.

## Розвідувальне: потужність інших дизайнів (лише SD і MDE, без середніх; вердикт не змінює)

Ця таблиця порахована **після** воріт і лише для планування. Вона показує, який дизайн міг би мати потужність. Вибирати за нею «найкращий» варіант для цієї картки не можна: варіанти після воріт не додаються.

| Дизайн | n | SD, п.п. | ICC | MDE_icc | MDE_cr |
|---|---|---|---|---|---|
| H = 7, ліквідація 1x | 541 | 35.40 | +0.008 | 4.53 | 5.72 |
| H = 7, ліквідація 3x (+33 % → −1/3) | 541 | 26.44 | +0.024 | 3.72 | 4.35 |
| **H = 14, ліквідація 1x (зареєстровано)** | 541 | 42.93 | +0.018 | 5.83 | 7.25 |
| H = 14, ліквідація 3x | 541 | 30.95 | +0.028 | 4.46 | 5.45 |
| H = 30, ліквідація 1x | 541 | 52.26 | +0.043 | 8.10 | 9.36 |
| H = 30, ліквідація 3x | 541 | 36.06 | +0.048 | 5.70 | 6.53 |
| H = 14, 1x, лише нові токени | 331 | 48.48 | +0.014 | 8.08 | 10.33 |

Конвенція «3x» обрізає хвіст сквізу і тому зменшує SD, але це вже інша стратегія: з плечем і втратою маржі при +33 %. Короткий горизонт теж зменшує SD, але й очікуваний дрейф за 7 діб, якщо він є, менший за 14-денний. Тож нижча MDE не означає кращого шансу знайти ефект.

## Застереження

1. **Funding не перевірено.** Механізм п. 2 картки («лонги платять шорту») лишається неперевіреним (умова 2). Аналітика для нього готова й протестована на реальному зразку: `funding_received` переоцінює номінал, `funding_gaps` враховує зміну інтервалу 8h → 4h. Архів funding містить колонку інтервалу, і в ньому є делістнуті символи: FTT, листопад 2022, ставка −2.5 % за 8 год.
2. **Ворота залежать від хвоста, а не від помилок даних.** Три найбільші денні рухи — реальні сквізи. Без них SD була б меншою, але викидати їх не можна: це саме той ризик, про який картка (H4).
3. **Вік токена за OKX** визначено за `listTime` нинішніх інструментів. Делістнуті з OKX пари не видно, тож частина «нових» токенів може бути старшою. На головну вибірку (усі події) це не впливає.
4. **Bybit, OKX і HL як окремі ринки лістингів** (перевірка з картки) не тестувались: ворота закрили дослідження раніше. Об'єднання бірж не дає незалежних подій: ті самі токени лістяться майже одночасно. MDE це покращило б мало.
5. **Навіщо ворота.** Кластерна SE середнього дорівнює 7.25 / 2.8 = 2.6 п.п. Тож значущим (5 %, двосторонньо) було б лише вибіркове середнє ≥ 5.1 п.п. Справжній ефект +5 п.п. дав би значущий результат лише приблизно в половині вибірок, а справжні +3 п.п. виглядали б як шум. Тест із такою потужністю дав би або хибне «ефекту немає», або випадкове «є», тому ворота зупиняють його до перегляду середніх.

## Що далі

- Для цієї картки: повторити стадію 0 приблизно у 2027-2028 роках, коли подій стане ≈ 750–1100. Реєстрація, аналітика і скрипт готові, кеш відновлюваний. Головну комірку не змінювати.
- Якщо власник захоче інше питання, як-от «шорт з плечем 3x на 7 діб», це нова картка з власною реєстрацією. Розвідувальна таблиця вище лише показує, що такий дизайн мав би потужність, і нічого не каже про знак ефекту.
- Провайдер `binance_vision` тепер є й у цій гілці. Він знадобиться карткам funding-crowding (survivorship лонг-ноги) і liquidation-flush. Якщо обидві гілки зіллються, буде конфлікт в одному файлі провайдера: тут додано лише метод funding.

## Сирий вивід

```
$ uv run python scripts/verify_listing_drift.py probe        (з --save-fixtures оновлює зразки в tests/fixtures/)
OK  /data/futures/um/monthly/klines/ARBUSDT/1h/ARBUSDT-1h-2023-03.zip: 10818 bytes
OK  /data/futures/um/monthly/fundingRate/ARBUSDT/ARBUSDT-fundingRate-2023-03.zip: 435 bytes
OK  /data/futures/um/monthly/fundingRate/FTTUSDT/FTTUSDT-fundingRate-2022-11.zip: 1161 bytes
OK  s3 listing data/spot/monthly/klines/ARBUSDT/1d/: 26321 bytes

$ uv run python scripts/verify_listing_drift.py meta
archive USDT-M symbols 1056; exchangeInfo symbols 927
eligible (USDT, COIN, no stables/wrapped/TradFi/index): 680
[listing 1d] done in 134s; transient errors: {}

$ uv run python scripts/verify_listing_drift.py fetch0
candidates (first archive month 2022-01..2026-09): 542
[fetch0] done in 109s; transient errors: {}

$ uv run python scripts/verify_listing_drift.py age
[age] 3410 files, 3222 to fetch
[age] done in 306s; transient errors: {}
[age spot months] 952 files, 912 to fetch
[age spot months] done in 139s; transient errors: {}

$ uv run python scripts/verify_listing_drift.py stage0
events (one per asset, first 1h trade in [2022-01-01; 2026-09-16)): 541
  note: duplicate asset BOB: BOBUSDT (first 2025-11) after 1000000BOBUSDT (first 2025-06) -> not an event
by year: {2022: 24, 2023: 98, 2024: 131, 2025: 238, 2026: 50}
status today: {'TRADING': 427, 'SETTLING': 104, 'REMOVED': 10}
absent from today's exchangeInfo (fully removed, from the archive only): 10: 1000BTTCUSDT AERGOUSDT ANCUSDT BDXNUSDT COCOSUSDT FRONTUSDT GALUSDT MBLUSDT MDTUSDT RNDRUSDT
onboardDate vs first 1h trade differ by > 2 days: 11
  AIAUSDT          onboard 2026-01-20 first trade 2025-09-18
  BNXUSDT          onboard 2023-02-22 first trade 2022-04-01
  CVXUSDT          onboard 2025-07-23 first trade 2022-09-22
  EDUUSDT          onboard 2023-04-28 first trade 2023-04-30
  HIGHUSDT         onboard 2023-02-03 first trade 2023-02-07
  HOOKUSDT         onboard 2023-01-19 first trade 2023-01-23
  LQTYUSDT         onboard 2023-03-08 first trade 2023-03-10
  MAVIAUSDT        onboard 2025-03-26 first trade 2024-02-21
  MINAUSDT         onboard 2023-02-03 first trade 2023-02-07
  PUMPUSDT         onboard 2025-07-10 first trade 2025-04-12
  SLPUSDT          onboard 2025-07-23 first trade 2023-10-31
first trade - onboardDate (h): median -0.42, min -24839.5, max 100.0
first-trade hour (UTC): {1: 1, 2: 18, 3: 19, 4: 5, 5: 8, 6: 9, 7: 17, 8: 33, 9: 37, 10: 45, 11: 56, 12: 111, 13: 54, 14: 65, 15: 29, 16: 15, 17: 11, 18: 5, 19: 3}
hours from first trade to entry open: min 29, max 47
market age before the Binance perp: {'<=30d': 331, '31-365d': 81, '>365d': 129}; by year: {'2022': {'<=30d': 6, '31-365d': 13, '>365d': 5}, '2023': {'31-365d': 14, '<=30d': 25, '>365d': 59}, '2024': {'31-365d': 31, '<=30d': 59, '>365d': 41}, '2025': {'<=30d': 196, '>365d': 21, '31-365d': 21}, '2026': {'<=30d': 45, '>365d': 3, '31-365d': 2}}
already traded on another perp (Bybit/OKX/HL) > 1 day before t0: 212 of 541
venue legs rejected by the price-level check (different token, same ticker): {'bybit': 10, 'binance': 1}

STAGE 0 metric: short price P&L (1x liquidation) + r_BTC - 0.25% cost, H=14, entry delay 0 (no funding)
evaluable events n=541 in 54 months (by year: {'2022': 24, '2023': 98, '2024': 129, '2025': 237, '2026': 53}); truncated exits 1
SD=42.93pp  ICC(month)=+0.0179  deff=1.273  n_eff=424.9
MDE_icc=5.83pp  MDE_cr=7.25pp  -> gate MDE=max=7.25pp (threshold 5pp)
GATE: n>=150: True; months>=24: True; MDE<=5pp: False -> FAIL
  2022-2023: n=122 SD=32.10pp
  2024-2026: n=419 SD=45.61pp
events needed for MDE <= 5pp at this SD/clustering: ICC route 736, CR1 route 1137 (MDE ~ 1/sqrt(n))
events needed for MDE <= 6pp at this SD/clustering: ICC route 511, CR1 route 789 (MDE ~ 1/sqrt(n))

check BTCUSDT perp close 2024-12-31 = 93548.9 (known ~93,500; token-unlock report 93,548.9)
check ARBUSDT first 1h trade 2023-03-23 15:00 UTC, onboardDate 2023-03-23 07:00 UTC (ARB airdrop and first trading 2023-03-23)
check 1d close == last 1h close on the first full day: 518/518 equal; differ: []
check missing daily bars inside the H=14 window: 1 events ['IMXUSDT:1']
check day-over-day close moves >= x3 or <= x1/3 inside the window (squeezes or data errors; verified by hand): ['AIAUSDT 2025-10-02 x3.15', 'COAIUSDT 2025-10-06 x6.21', 'PNUTUSDT 2024-11-13 x3.82']

PLANNING ONLY (exploratory, not registered, no means): power of other designs on the same events
  H=7  liquidation 1x (-100%)                  n= 541 months= 54 SD= 35.40pp ICC=+0.008 MDE_icc= 4.53pp MDE_cr= 5.72pp
  H=7  liquidation 3x at +33% (-1/3)           n= 541 months= 54 SD= 26.44pp ICC=+0.024 MDE_icc= 3.72pp MDE_cr= 4.35pp
  H=14 liquidation 1x (-100%)                  n= 541 months= 54 SD= 42.93pp ICC=+0.018 MDE_icc= 5.83pp MDE_cr= 7.25pp
  H=14 liquidation 3x at +33% (-1/3)           n= 541 months= 54 SD= 30.95pp ICC=+0.028 MDE_icc= 4.46pp MDE_cr= 5.45pp
  H=30 liquidation 1x (-100%)                  n= 541 months= 54 SD= 52.26pp ICC=+0.043 MDE_icc= 8.10pp MDE_cr= 9.36pp
  H=30 liquidation 3x at +33% (-1/3)           n= 541 months= 54 SD= 36.06pp ICC=+0.048 MDE_icc= 5.70pp MDE_cr= 6.53pp
  H=14 1x, new tokens only (market <= 30 d)    n= 331 months= 47 SD= 48.48pp ICC=+0.014 MDE_icc= 8.08pp MDE_cr=10.33pp
  H=14 1x, share of evaluable events per year: {'2022': 24, '2023': 98, '2024': 129, '2025': 237, '2026': 53}
```
