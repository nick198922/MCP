# 2026-11-24 · Заклики @btc_charlie, 2021-Q1 (партія пункту 2, поза вибіркою 3.3)

Картка: [IDEA-20261113-x-traders](../ideas/IDEA-20261113-x-traders.md), пункт 2 і розділ 3.3. Профіль: [LIB-20261114-x-btc_charlie](../library/LIB-20261114-x-btc_charlie.md). Шаблон методу: [2021-Q2](2026-11-24-x-calls-btc_charlie-2021q2.md), [2020-Q3](2026-11-23-x-calls-btc_charlie-2020q3.md). Результат: **21 рядок** з 19 твітів дописано в кінець [`docs/calls/x/btc_charlie.csv`](../calls/x/btc_charlie.csv), після блоку 2021-Q2; коментар блоку стоїть у шапці файлу після коментаря 2021-Q2. У першій версії було 18 рядків з 16 твітів; три рядки (GME 01-27, ETH 03-07, HTR 03-15) дописано за рев'ю B 24.11, п. 1, див. «Правки за рев'ю». Розмитих: (а) ≈ 26, (б) 14, (в) ≈ 60. Ціни ринку не завантажувались, бектесту не було. Кожен рядок має тег «3.3 (24.11, партія поза вибіркою 3.3; до цін; лише твіти рядка ≤ t0)»: розмітка за правилами 3.3 / 3.3б, але **лише описова**.

## Чому цей квартал і чому він не входить до вибірки 3.3
- **Завдання координатора 24.11:** узяти наступний квартал у порядку 3.3. Після 2022-Q1, 2020-Q3 і 2021-Q2 це **2021-Q1** (порядок 4, 45 порожніх днів з 90).
- **Розбіжність з рішенням у журналі.** Рядок журналу 24.11 («рішення для координатора», рев'ю B 24.11, п. 7) радив не брати інших кварталів поза префіксом, бо для рішення вони нічого не дають. Квартал узято лише тому, що координатор прямо доручив цю партію. Її статус такий самий, як у 2021-Q2: **довідкова**. Рядок журналу 24.11 як найближчий довідковий називав 2021-Q3, але за порядком 3.3 (менше порожніх днів — раніше) після 2021-Q2 іде 2021-Q1 (45 днів проти 52).
- **Наслідок.** Рядки не входять ні до первинного набору, ні до `btc_charlie-eval.csv` і вердикту 3.3в (✗) не змінюють. Код бере до `main` лише квартали префікса (`MAIN_QUARTERS`), тож нові рядки потрапляють у «outside both samples». Стадія 0 (без свічок, 24.11) це підтверджує: `rows outside both samples (not evaluated): 38` (17 рядків 2021-Q2 + 21 новий), `main` без змін (101 рядок, первинний 64, W 27). Код не змінювався.

## Дані
- Архів `docs/library/courses/x-btc_charlie/tweets.json` (недовірені дані, поза git): **sha256 `efb652ffb2ac`**, 26 195 твітів. `state.json`: sha256 `1da2f6affaf9`, січень–березень 2021 є в `done_months`. Обидва ті самі, що в 2021-Q2.
- Зріз 2021-01-01 … 2021-04-01 UTC: **802 твіти** (січень 360, лютий 260, березень 182). З них 476 відповідей іншим, 58 самовідповідей, 14 ретвітів, 254 оригінали; 53 цитують, 263 мають медіа.
- **45 порожніх днів:** 01–14.01, 01–17.02, 01–05.03, 08–13.03, 20.03, 22.03, 25.03. Розриви: 31.12 22:38 → 15.01 08:03 (345 год), 31.01 23:27 → 18.02 10:26 (419 год), 28.02 22:14 → 06.03 19:12 (141 год), 07.03 09:17 → 14.03 17:17 (176 год), а також три коротші в березні (43–54 год). Квартал закінчується розривом 31.03 21:04 → 20.04 18:33. Великі розриви — **пропуск збору**. Власні твіти з них відомі з цитат (01, 05, 10, 11, 13.01; 01, 06, 09.02; 02.03), а самовідповіді 07.03 і 30.03 посилаються на твіти 02.03 і 12.03, яких в архіві нема. Два відновлені власні твіти мають дати в **непорожніх** днях: 15.01 13:26 «$LINK $TRB $BAND $ORAI Full send.» (збір відновився 15.01 о 08:03) і 17.01 09:43 «They call me Neville because all I do is long bottoms» (рев'ю B 24.11, п. 2).
- **Відновлено з цитат 14 твітів (2 з них — у непорожніх днях 15.01 і 17.01), рядків з них 1** (CHR 02.03 — див. таблицю, виключено як відновлений). Решта: 01.01 «Goals for 2021: Accumulate $RUNE, $RSR and $DOT on all dips…» (в); 05.01 «I paid more than I wanted to for $TVK» (а); 10.01 і 11.01 «Send $ALTS to valhalla by Friday» (в); 13.01 «$ALT roadmap» (в); 13.01 HTR «Remind me of the early $DOT charts» (в); 15.01 «$LINK $TRB $BAND $ORAI Full send.» (в); 17.01 «all I do is long bottoms» (в); 01.02 треди «How I trade» і 06.02 «Relative Strength» (в); **01.02 «Aped into $XMON last night»** — скриншот чату й графік без ціни, (а); 09.02 «My learnings» (в); 09.02 «100x leveraged apes are not safe» (в).
- **Відсутніх батьків самовідповідей 19.** Більшість — у розривах або раніше за квартал. Три з них (16.01 10:22, 10:28, 16:34) **усередині зібраного дня**, тож неповнота є і в непорожніх днях.
- **Посилань `t.co` без цитати 10 (в 11 твітах) не розкрито:** партія йшла без мережі. За текстом жоден із цих твітів не схожий на торговий (музика, мем «100 trades a day», AIOZ, «If you're not on FTX», відповіді). Розкрити їх можна `bc_tco.py`, коли буде потреба.
- **Відбір тексту** (`bc_q_filter.py`): прочитано 467 твітів, пропущено 335 (ретвіти без фото й короткі відповіді без чисел). У пропущених пошук торгових слів (`bc_21q1_skipkw.py`) дав 20 збігів. З них важливі: «Got filled on some stink-bids this morning» (22.02, без цін — (а)), «Got lucky with a LINA breakout play that I didn't close» (18.03, продовження), «had a % or two of the supply and sold it all for a loss» (31.01, SHIB). Решта — розмови.
- **Фото:** 287 файлів у 267 твітах, 13 побайтових дублікатів, 12 `quoted.media` відновлених твітів. Разом **282 унікальні картинки, переглянуто всі** на 10 контактних аркушах 6×5 (340 px). У рідній роздільності переглянуто: нижні частини 7 карток FTX (`c1`), шапки TradingView 22 графіків (`h1`, `h2`), повні графіки BTC 24.02 і FTX BTC-PERP 29.01 (`z1`).
- **Стиль кварталу:** розпал альтсезону. Багато лонгів альтів «LTF breakout», мемкоїнів (DOGE, XRP) і GME, а BTC торгує в обидва боки. Карток FTX з цінами 6 (SXP ×2, ETH, DOT, BTC ×2), ще одна, DOGE 28.01 (+1,587.25 %), без цін. У березні майже нема власних заходів: «My next 100X $ALT: $AIOZ» (IDO 2 квітня, ринку на t0 нема), SOL «I'm in from much lower», продажі частин (LINA, AXS).

## Критерії
Ті самі, що в [2021-Q2](2026-11-24-x-calls-btc_charlie-2021q2.md): джерела — лише твіти з `note`, датовані днем рядка й не пізніші за t0; t0 = `call_moment` (перший id у `note` того дня); пріоритет цін «картка > текст > графік»; поріг «не за ринком» 0.5 %; визначення умовного тригера, таймфрейму й скальпу — з 3.3. Ціни з картинок — лише з підписів (картка FTX: Avg buy / sell price, Mark price; шапка TradingView: C), позначено «з графіка». Нових правил відбору нема. Застосування до цього кварталу:
- **Картка FTX з цінами → рядок, показано вже в плюсі (п. 1).** SXP 01-18, ETH 01-19, DOT 01-20, BTC 01-21, SXP 01-26, BTC 02-22 (хедж). Картку DOGE 28.01 без цін віднесено до (а).
- **Шаблон Q-20261118-2 → рядок з позначкою «межовий за 3.3: шаблон…» (альт. (а)).** Їх 10: AKRO 01-20, ADA, CHZ і LTC 01-20 (один твіт, три графіки), BTC 01-25, SXP 01-31, DOGE 02-19 (без названої позиції — «LTF Scalp - $DOGE About to pop again»; альт. (а) / (в)), BTC 02-22 («Closed short and flipped long»), BADGER 02-22, CHR 03-02 (відновлений).
- **Умовний тригер з ціною на графіку → рядок з виключенням «умовний»:** COTI 02-26 «I'll buy 1/3 on a close like this».
- **Лімітний намір з ціною в тексті → рядок:** DOT 02-26 «I'd top up at $25» (20.9 % нижче ринку). Позначено «межовий»: «I'd» — бажання з числом, альт. (б).
- **Розкриття з ціною входу в тексті → рядок, навіть якщо дата входу невідома** (прецеденти `BC-2021-04-30-ALPHA-LONG`, `BC-2022-01-23-LRC-LONG`, `BC-2021-06-13-BTC-LONG`; додано за рев'ю B 24.11, п. 1). Група за поточною ціною з твіту: HTR 03-15 «Buys at $0.18 are 8X» — показано вже в плюсі (C 1.5848 з графіка); ETH 03-07 «Rebought $ETH at $1,500» — стан невідомий (на графіку лише ETHBTC, ціни в USD нема; межовий: альт. (а), дата входу невідома); GME 01-27 «YOLO buy at $200» — стан невідомий, виключення TradFi (як SPX 2026-01-29, OIL / SPCX 2026-Q3). Виняток: SOL 28.03 «Lowest buys $1.56 / Highest buys $13.29 / Avg: $5.13» лишається в (а), бо це середнє накопичення за невідомий час у діапазоні 1.56–13.29, а не один вхід: у рядку не було б однієї зони входу і моменту, до якого її прив'язати.
- **Розкриття без підпису ціни → (а), не рядок.** BTC 24.02 «Red arrow is where I started shorting (again)» (стрілка без підпису; план «Close short. Buy spot.» і «Add to spot. Open leverage long.» — зони без чисел). BTC 29.01 «Neville'd $BTC this morning» (стрілки на 1-хв графіку FTX без підпису). Ціну зі шкали осі не читали.
- **Закриті угоди, видимі лише при виході → окремий лічильник, не рядки** (прецедент 2020-Q2).

## Підсумок
| Твітів в архіві | Відновлено з цитат | Конкретних рядків | ex ante | з них шаблон Q-20261118-2 | показано вже в плюсі | стан невідомий | Розмитих |
|---|---|---|---|---|---|---|---|
| 802 (360 · 260 · 182; 45 порожніх днів) | 14 (рядків 1) | 21 з 19 твітів | 12 | 10 | 7 | 2 | (а) ≈ 26 · (б) 14 · (в) ≈ 60 |

| Рядок | Група (3.3) | Власність | Свінг / скальп | Виключення | Джерело ціни |
|---|---|---|---|---|---|
| `BC-2021-01-18-SXP-LONG` | показано вже в плюсі (картка +224.07 %) | строго власна | свінг (графік 360) | — | картка FTX 1.0220 (mark 1.1365) |
| `BC-2021-01-19-ETH-SHORT` | показано вже в плюсі (картка +296.75 %) | строго власна | свінг (графік 3D) | — | картка FTX 1,418.7 (mark 1,376.6) |
| `BC-2021-01-20-AKRO-LONG` | ex ante (шаблон, m = 0; межовий) | строго власна («Buying some more») | скальп (графік 60) | — | графік, C 0.015810 |
| `BC-2021-01-20-ADA-LONG` | ex ante (шаблон, m = 0; межовий) | строго власна («Bought») | скальп (графік 60) | — | графік, C 0.34855 |
| `BC-2021-01-20-CHZ-LONG` | ex ante (шаблон, m = 0; межовий) | строго власна | скальп (графік 60) | — | графік, C 0.021882 |
| `BC-2021-01-20-LTC-LONG` | ex ante (шаблон, m = 0; межовий) | строго власна | скальп (графік 60) | — | графік, C 145.77 |
| `BC-2021-01-20-DOT-LONG` | показано вже в плюсі (картка +90.35 %) | строго власна | свінг (графіка нема) | — | картка FTX 16.107 (mark 16.835) |
| `BC-2021-01-21-BTC-SHORT` | показано вже в плюсі (картка +376.06 %) | строго власна | свінг (графік 1D) | — | картка FTX 38,675 (mark 31,403) |
| `BC-2021-01-25-BTC-LONG` | ex ante (шаблон, m = 0; межовий) | строго власна («Punting a long here») | скальп (графік 15) | — | графік, C 33,588.93 |
| `BC-2021-01-26-SXP-LONG` | показано вже в плюсі (картка +64.50 %) | строго власна | свінг (графіка нема) | — | картка FTX 0.9697 (mark 1.0010) |
| `BC-2021-01-27-GME-LONG` | стан невідомий (поточної ціни нема; межовий) | строго власна («my $GME YOLO buy») | свінг (графіка нема) | **TradFi** (акція NYSE) | текст (200) |
| `BC-2021-01-31-SXP-LONG` | ex ante (шаблон, m = 0; межовий) | строго власна («one little long») | скальп (графік 5) | — | графік, C 1.3255 |
| `BC-2021-02-19-DOGE-LONG` | ex ante (шаблон, m = 0; межовий) | без названої позиції | скальп («LTF Scalp», графік 15) | — | графік, C 0.0568575 |
| `BC-2021-02-22-BTC-SHORT` | показано вже в плюсі (картка +7.73 %, 3X) | строго власна (хедж) | свінг (3X) | — | картка FTX 57,812 (mark 56,323) |
| `BC-2021-02-22-BTC-LONG` | ex ante (шаблон, m = 0; межовий) | строго власна («flipped long») | свінг (графік 240) | — | графік, C 53,576 |
| `BC-2021-02-22-BADGER-LONG` | ex ante (шаблон, m = 0; межовий) | строго власна («Bought some») | скальп (графік 15) | — | графік, C 54.055 |
| `BC-2021-02-26-COTI-LONG` | ex ante (умовний тригер) | з наміром («I'll buy 1/3») | скальп (графік 60) | **умовний** («on a close like this») | графік, C 0.22458 |
| `BC-2021-02-26-DOT-LONG` | ex ante (не за ринком 20.9 %; межовий) | з наміром («I'd top up») | свінг (графік 240) | — | текст (25); поточна C 31.6072 з графіка |
| `BC-2021-03-02-CHR-LONG` | ex ante (шаблон, m = 0; межовий) | з наміром («Yes I'll have a small bag») | свінг (графік 240) | **відновлений** (цитата 07.03) | графік, C 0.05360 |
| `BC-2021-03-07-ETH-LONG` | стан невідомий (ціни в USD нема; межовий) | строго власна («Rebought») | свінг (графік 1D) | — | текст (1,500); графік ETHBTC, C 0.0337650 BTC |
| `BC-2021-03-15-HTR-LONG` | показано вже в плюсі (m ≈ +780 %, «8X») | строго власна («I haven't sold any yet») | свінг (графік 1D) | — | текст (0.18); поточна C 1.5848 з графіка |

- **Активи:** SXP 3, BTC 4, DOT 2, ETH 2, AKRO, ADA, CHZ, LTC, DOGE, BADGER, COTI, CHR, GME, HTR. **Сторони:** лонгів 18, шортів 3 (ETH 01-19, BTC 01-21, BTC 02-22 — усі з карток у плюсі). Цілей і стопів з твітів ≤ t0 нема в жодному рядку. Пар до BTC нема (SXP 01-18 має графік SXPBTC, але ціна з картки SXP-PERP у USD; ETH 03-07 має графік ETHBTC, але вхід $1,500 з тексту в USD).
- **Час графіків шаблону** (шапка TradingView «published … GMT», до 28.03 GMT = UTC, проти часу твіту за snowflake): AKRO 11:12:30 → 11:15:28; ADA 14:24:22, CHZ 14:26:02, LTC 14:27:42 → 14:29:41; BTC 01-25 19:58:03 → 19:58:44; SXP 15:49:14 → 15:52:23; DOGE 12:32:34 → 12:34:24; BTC 02-22 12:28:28 → 12:29:09; BADGER 21:57:57 → 21:59:40; CHR 20:10:03 → 20:13:17. Усі ≤ 10 хв (найдовше — ADA, 5.3 хв). Поточна ціна DOT 02-26 узята з графіка 15:08:35, тобто за 7.5 хв до твіту 15:16:07; на цьому ж графіку (≤ t0) стоїть червона горизонталь з підписом 25.6132 — рівень добирання (рев'ю B, п. 4): за пріоритетом «текст > графік» вхід 25 (20.9 %), з 25.6132 було б 19.0 %, група та сама, але лінія — аргумент за читання «план / ордер». Поточна ціна HTR 03-15 — з графіка 08:16:12 (твіт 08:16:34).
- **t0 і правило «≤ t0».** Для всіх 21 рядка t0 тегу збігається з `call_moment` проєкту (`bc_21q1_append.py` для перших 18, `bc_21q1_fixcheck.py` для всіх 21: «ok=True» для 21 з 21). Перенесень t0 нема. «Змінено правилом t0» — 0. Пізніші твіти мітки не змінюють. Прийнято навіть ті, що увімкнули б п. 1 чи показали б збиток: DOGE 02-19 16:03 «Did not work», ADA / LTC / CHZ 22.01 «breakeven… down some on CHZ», CHR 07.03 «+90%», AKRO 07.03 «+27%» і 30.03 «+158%».
- **Якби квартал входив до 3.3 (довідково, лічильники без цін):** ex ante без виключень (без COTI й CHR) 10 рядків у 4 ISO-тижнях (W03, W04, W07, W08). Тоді k = 6. На лічильниках стадії 0 (первинний 64, W = 27) — 74, W = 31, W₁₂ = 31 · 12 / 6 = 62 < 99. Рішення контрольної точки (зупинка) не змінилося б і з 2021-Q2 разом (k = 7: 82, W 37, W₁₂ ≈ 63). Три рядки, дописані за рев'ю (GME, ETH 03-07, HTR), не ex ante, тож ці числа ті самі.

### Розмиті
- **(а) розкриття без ціни входу — ≈ 26** (у першій версії ≈ 28: GME 27.01 і ETH 07.03 стали рядками за рев'ю B, п. 1). Серед них:
  - `1351250605726171140` (18.01) — список позицій FTX без цін (SUSHI, DOT, EGLD, HT, LINK, SXP, THETA, YFI — лонги, BTC — шорт);
  - `1351846274757255168` (20.01) «$VET's actually one of my biggest bags»;
  - `1352658343173775361` (22.01) «I'm short btc long alts»;
  - `1352666573941706752` (22.01) «my atom trade»;
  - `1353787407926177792` (25.01) «balls deep in $ALTS»;
  - `1354808001635336194` (28.01) картка FTX DOGE-PERP Long +1,587.25 % без цін;
  - `1354946069130596357` (29.01) «Rolling some $DOGE profits into $XRP»;
  - `1355121698698366976` (29.01) «Neville'd $BTC this morning» (стрілки без підпису);
  - `1355467008440217600` (30.01) «Scalping $DOGE has been intense»;
  - відновлений 01.02 XMON «Aped into $XMON last night»;
  - `1362668255253561351` (19.02) «Re-balanced some of my binance bags»;
  - `1363809546024714246` (22.02) «Got filled on some stink-bids»;
  - `1363827067347959809` (22.02) KOTH;
  - `1363858586372362240` (22.02 14:30) «Buying $BTC. Buying $ALTs.» (мем «LONG» без графіка);
  - `1363950333463064584` (22.02) «I bought $FTT»;
  - `1363809997298229252` (22.02) «bags of $YVS, $JUL and $JULD»;
  - `1364510917476646916` (24.02) «I swapped some shitcoins into $RUNE yesterday»;
  - `1364600568644325380` (24.02) BTC «Red arrow is where I started shorting (again)»;
  - `1366064342144323596` (28.02) «Building my position in that super coin»;
  - `1372556191914676225` (18.03) «Long from when Magus… said "Long SNX"»;
  - `1372447628441571333` (18.03) «decent bag of SRM»;
  - `1372808993417129985` (19.03) WCRES «originally bought in December»;
  - `1375723270511734787` (27.03) SOL «I'm in from much lower»;
  - `1376108853331431427` (28.03) SOL «Lowest buys $1.56 / Highest buys $13.29 / Avg: $5.13» — у (а), а не рядок: це середнє накопичення за невідомий час у діапазоні 1.56–13.29, а не один вхід, тож нема ні однієї зони входу, ні моменту (на відміну від ALPHA / LRC / HTR з однією ціною);
  - відновлений 05.01 TVK «I paid more than I wanted to».
- **(б) план без входу — 14:**
  - `1350061881784999937` SXP «too much Port to justify taking a position» (15.01);
  - `1352907287577440256` «roll some trading profits into» low caps (23.01);
  - `1353043333002780673` THETA «Wait for confirmation or just yolo long?» (23.01);
  - `1353052936717398017` «just going to long into resistance» (23.01, без інструмента й ціни);
  - `1353621962766094337` SXP «looking for a close above the range… I'll pick some spot up» (25.01);
  - `1363961960858079232` HEGIC «might finally get a decent entry here. Note: no trigger = no trade» (22.02; графік ChartEx UNISWAP:HEGIC 1D, тригер не названо);
  - `1363966521274474498` TOMO «still got to close; but looking good for some spot buys» (22.02);
  - `1364668694421508097` REEF «If not will be looking for adds below» (24.02);
  - `1364875457355780100` OCEAN «I want $OCEAN to drop. So I can buy» (25.02);
  - `1365323783473680391` RSR «I'd rather buy consolidation after confirmed strength» (26.02);
  - `1368481095772168193` BNB «Looking for something like this to get involved» (07.03);
  - `1371772137434968068` «Plan: keep longing alts» (16.03);
  - `1372209176030687235` LINA «Been watching $LINA for this all day. Glorious.» (17.03; позицію названо лише 18.03, після t0);
  - `1376622906181050369` AIOZ-тред «My next 100X $ALT» (29.03; IDO 2 квітня, ринку на t0 нема).
- **(в) погляди, рівні, меми — ≈ 60** (евристика, ±15). Серед них: «Trading vs. holding», CRV / TOMO «take out the ATH», XRT, KSM «$1,000», ETH «$2,000», RSR «bullish re-test», HBAR, ETH «Renko», ICX «undervalued», «Are we buying this $ALT or nah?», RUNE / LINK «leading the way», ATOM «flies through», AAVE «marked this earlier», THETA «not a fakeout», «Liking $MATIC and $ADA here», BCH «scam pump», TRX, SXP «Send it already» / «Just the beginning», WAVES, DOGE «Gearing up for the next leg», LINK, RVN, ALGO, BTC «strong uptrend», SOL / SRM / FTT, SRM/BTC «cup and handle», BAND, LUNA, FTM, RFUEL, 1INCH, FIL, RSR «$1», TVK «Anything under $1 is FUD», індикатор BTC, спагеті, «$ALTS to valhalla», «Total $ALT market cap to 2x», AIOZ-тред (12 твітів), RSR/BTC «This time, we fly», KSM / DOT тощо.
- **Закриті угоди, видимі лише при виході (окремий лічильник, не рядки) — 6:**
  - `1353947825340633089` «$SXP Original trade stopped» (26.01; **збиток**, без цін; нова SXP — рядок 01-26);
  - `1354954925151821826` «only 1/3 position. Sold it for 40%» (29.01, інструмент у відповіді не названо);
  - `1355973343225249794` + `1356012120899514371` SHIB «Sold it all for a loss» (31.01; **збиток**, позиція з серпня 2020);
  - `1364580743456915457` «Sold all my $ETH» (24.02; 07.03 — «Sold it all at $1,600~»);
  - `1368465986182119426` «Put it in $DOT and flipped for +15%» (07.03);
  - `1363962265398034436` TOMO «convinced me and I sold it all» (22.02).
- **Продовжень ≈ 24** (у першій версії ≈ 25: HTR 15.03 став рядком за рев'ю B, п. 1). До рядків цього кварталу: ETH 20.01 «short closed» / «from $1.4k~ closed around $1.32k», ADA / LTC / CHZ 22.01, SXP 27.01 «Thank you», DOGE 19.02 «Did not work», BTC 26.02 «Still in my shorts» і 28.02 «So far so good» (шорт 24.02, (а)), CHR 07.03. Інші: KSM 16.01 і 18.01 «Up nearly 2x from the adds», HTR 19.01, 25.02, RSR/XRP 29.01, портфель 29.01 («$RUNE +3,119%…»), XRP 30.01, FTT vs BNB 19.02 «Taking a little off», XMON 21.02 «up around 25x», TVK 25–26.02, RSR / XED / AKRO / DIA 07.03 (батьків нема в архіві), LINA 18.03 «Shaving 20% off» і «didn't close», AKRO 30.03, AXS 30.03 «Selling half», SOL 29.03.

## Межові за 3.3 (до цін; описово — рядки поза вибіркою)
| Рядок | Первинна мітка | Альтернатива | Причина |
|---|---|---|---|
| `BC-2021-01-20-AKRO-LONG` | ex ante (шаблон), рядок | (а), не рядок | шаблон Q-20261118-2 |
| `BC-2021-01-20-ADA-LONG` | ex ante (шаблон), рядок | (а), не рядок | шаблон Q-20261118-2; «Bought» — минулий час |
| `BC-2021-01-20-CHZ-LONG` | ex ante (шаблон), рядок | (а), не рядок | шаблон Q-20261118-2 |
| `BC-2021-01-20-LTC-LONG` | ex ante (шаблон), рядок | (а), не рядок | шаблон Q-20261118-2; «shocking entry» — вхід, можливо, не за міткою графіка |
| `BC-2021-01-25-BTC-LONG` | ex ante (шаблон), рядок | (а), не рядок | шаблон Q-20261118-2 |
| `BC-2021-01-31-SXP-LONG` | ex ante (шаблон), рядок | (а), не рядок | шаблон Q-20261118-2 |
| `BC-2021-02-19-DOGE-LONG` | ex ante (шаблон), рядок | (а) / (в), не рядок | шаблон без названої позиції («LTF Scalp… Not financial advice») |
| `BC-2021-02-22-BTC-LONG` | ex ante (шаблон), рядок | (а), не рядок | шаблон Q-20261118-2 |
| `BC-2021-02-22-BADGER-LONG` | ex ante (шаблон), рядок | (а), не рядок | шаблон Q-20261118-2 |
| `BC-2021-01-27-GME-LONG` | стан невідомий, рядок (TradFi, виключено) | показано вже в плюсі | «out-performing my crypto» — у плюсі за текстом, але без числа |
| `BC-2021-02-26-DOT-LONG` | ex ante (не за ринком), рядок | (б), не рядок | «I'd top up at $25» — бажання з числом, не ордер; лінія 25.6132 на графіку ≤ t0 говорить за план / ордер |
| `BC-2021-03-02-CHR-LONG` | ex ante (шаблон), рядок, відновлений | (а), не рядок | шаблон Q-20261118-2; твіт відомий лише з цитати |
| `BC-2021-03-07-ETH-LONG` | стан невідомий, рядок | (а), не рядок | дата входу невідома (між 24.02 і 07.03) |

Межових 13 (11 у першій версії + GME і ETH 03-07).

## Застереження
- **Поза вибіркою.** Рядки 2021-Q1 не змінюють ні розмітки `btc_charlie-eval.csv`, ні вердикту 3.3в (✗). Будь-яке використання цих рядків для оцінки — нова картка з власною пре-реєстрацією, яка враховує, що розмітку зроблено після відомого результату.
- **Неповнота.** 45 порожніх днів, зокрема перші два тижні січня й половина лютого (пік альтсезону). Три відсутні батьки 16.01 і два відновлені з цитат власні твіти в непорожніх днях (15.01 13:26 «Full send», 17.01 09:43 «Neville») показують, що неповнота є й усередині зібраних днів.
- **Асиметрія виграш / програш.** Усі 6 карток з цінами — у плюсі (+7.73 % … +376.06 % ROE), DOGE без цін — теж (+1,587.25 %). Збитки видно лише без цін: SXP «Original trade stopped», SHIB «sold it all for a loss», CHZ «down some», DOGE 19.02 «Did not work». Як і в 2020-Q3 і 2021-Q2, картки — відбір на виграш. Розкриття з ціною в тексті теж у плюсі або без стану: HTR 03-15 «8X» — у плюсі, GME і ETH 03-07 — стан невідомий. Є й відбір проти виграшу (рев'ю B, п. 5): шорт BTC 24.02 «Red arrow is where I started shorting (again)» стоїть у (а), бо стрілка без підпису ціни. Стрілка стоїть над останніми 4H-свічками (шапка графіка BINANCE:BTCUSDT · 240 15:26:36 GMT: O 50,360.02, H 51,294, C 49,065.69; перевірено на картинці), тож за читанням осі рядок був би «у плюсі» або межовим, а автор пізніше подав цю угоду як виграш (26.02 «Still in my shorts», 28.02 «So far so good»). Правило «лише підписи» правильно дає (а), але так з асиметрії випадає один виграш.
- **Плече з ROE.** На картках SXP 01-18 (≈ 20×), ETH (≈ 100×), DOT (≈ 20×), BTC 01-21 (≈ 20×), SXP 01-26 (≈ 20×) плече не підписане. За визначенням 3.3 скальп за плечем рахується лише з підписаного плеча, тому ці рядки — свінг (за графіком або без графіка). BTC 02-22 має підписане 3X.
- **Тікери.** `symbol` порожній у всіх 21 рядку (рев'ю B, п. 3). Картки й графіки: FTX-перпи SXP, ETH (01-19), DOT (01-20), BTC, DOGE, BADGER; Binance-спот AKRO, ADA, CHZ, LTC, COTI, CHR і ETHBTC (ETH 03-07, вхід рядка — $1,500 з тексту); KuCoin-спот HTR; GME — акція NYSE (TradFi, виключено). Стадію 0 зі свічками для цих рядків не запускали, бо вони поза вибіркою.
- **Ціни з графіків:** C у шапці TradingView для 10 рядків шаблону, COTI і поточної ціни DOT 02-26. Шкалу осі не читали: стрілки BTC 24.02 і 29.01 без підпису — (а). У `note` усіх таких рядків стоїть позначка «з графіка».
- **Скальпи** (AKRO, ADA, CHZ, LTC, BTC 01-25, SXP 01-31, DOGE, BADGER, COTI) визначено за правилом «інтервал графіка ≤ 1 год». ADA / CHZ / LTC за текстом — «small bags», тобто це правило механічне.

## Правки за рев'ю B (24.11)
[Рев'ю](../reviews/2026-11-24-x-calls-btc_charlie-2021q1.md), вердикт `request changes`. Кожну знахідку звірено з архівом і картинками перед правкою.
- **П. 1 (блокер), прийнято.** Прецеденти ALPHA 2021-04-30, LRC 2022-01-23 і BTC 2021-06-13 справді роблять рядок з розкриття з ціною входу в тексті, навіть коли дата входу невідома. Перша версія цієї партії від них відійшла. Дописано `BC-2021-01-27-GME-LONG` (стан невідомий, TradFi, межовий: альт. «показано вже в плюсі»), `BC-2021-03-07-ETH-LONG` (стан невідомий: на картинці лише ETHBTC 1D, C 0.0337650 BTC; межовий: альт. (а)) і `BC-2021-03-15-HTR-LONG` (показано вже в плюсі: KUCOIN:HTRUSDT 1D 08:16:12 GMT, C 1.5848, m ≈ +780 %). Перевірено картинки ETH і HTR у рідній роздільності та тексти й час трьох твітів в архіві. Батьки HTR (19.01 `1351602412633649156` і корінь гілки 13.01 23:50 `1349504217652879364`, відновлений, (в)) рядками не були, тож «продовження» було помилкою. SOL 28.03 лишається в (а), причину записано в «Критеріях» і в списку (а). Лічильники: рядків 18 → 21 (твітів 16 → 19), у плюсі 6 → 7, стан невідомий 0 → 2, межових 11 → 13, лонгів 15 → 18, (а) ≈ 28 → ≈ 26, продовжень ≈ 25 → ≈ 24; ex ante 12 і шаблонних 10 без змін. Стадія 0: `rows 216`, `outside both samples` 38 (як очікувало рев'ю), `main` і `combo` без змін.
- **П. 2, прийнято.** Відновлені 15.01 13:26 і 17.01 09:43 мають дати в непорожніх днях (перевірено датами цитованих твітів в архіві). Дописано в «Дані» і «Застереження».
- **П. 3, прийнято.** `symbol` порожній у всіх рядках блоку; перелік бірж і інструментів виправлено в шапці CSV і в «Застереженнях».
- **П. 4 (необов'язково), прийнято.** Лінію 25.6132 на графіку DOT 02-26 згадано в `note`, у причині межового і вище в розділі «Підсумок».
- **П. 5, прийнято.** Речення про BTC 24.02 як виграш, що випав через правило «лише підписи», дописано в «Застереження» (асиметрія).
- **П. 6, взято до відома.** Наступну довідкову партію btc_charlie поза префіксом (2021-Q3 тощо) не брати без нової картки з власною пре-реєстрацією: друга довідкова партія поспіль нічого не дала для рішення.
- **П. 7.** Зміни в `IDEA-20261111-profit-search.md` до цієї партії не належать, у цьому кроці їх не чіпав.

## Інструменти (поза git, `~/.cache/tradedesk-agents/tmp/`)
`bc_21q1_fixcheck.py` (перевірка всіх 21 рядка після правки за рев'ю: t0 проти `call_moment`, `parse_tag`, `parse_calls` усього файлу), `bc_q_dump.py 2021 1`, `bc_q_filter.py`, `bc_21q1_skipkw.py` (пропущені з торговими словами), `bc_q_sheets.py 2021 1 6 5 340` (аркуші `~/.cache/tradedesk-agents/x-calls-btc_charlie-2021q1/sheets/all-01…10.jpg`), `bc_21q1_crop.py` (фрагменти в рідній роздільності `c1`, `h1`, `h2`, `z1`), `bc_21q1_append.py` (запис рядків і коментаря блоку, перевірка t0 через `call_moment`, `parse_tag` і `parse_calls`).

### Сирий вивід `bc_q_dump.py 2021 1` (скорочено)
```
state sha256 1da2f6affaf9 done 2018-01 ... 2026-10 106
q months in done: [True, True, True]
tweets.json sha256 efb652ffb2ac archive total 26195
Q tweets 802 [('2021-01', 360), ('2021-02', 260), ('2021-03', 182)]
first 2021-01-15T08:03:18Z last 2021-03-31T21:04:41Z
gaps > 30 h touching Q: [('12-31T22:38', '01-15T08:03', 345.4), ('01-31T23:27', '02-18T10:26', 419.0), ('02-28T22:14', '03-06T19:12', 141.0), ('03-07T09:17', '03-14T17:17', 176.0), ('03-19T16:28', '03-21T11:03', 42.6), ('03-21T11:03', '03-23T17:19', 54.3), ('03-24T14:56', '03-26T18:19', 51.4), ('03-31T21:04', '04-20T18:33', 477.5)]
replies to others 476 self replies 58 retweets 14 originals 254
quoted 53 media 263
empty days 45
tweets with media 263 photo files 287 tweets with files 267
recovered own quotes 14 [('2021-01', 8), ('2021-02', 5), ('2021-03', 1)]
missing parents 19
lost-quote tweets 11 links 10
qmedia files 8
```
`bc_q_filter.py`: `keep 467 … skip 335`; `bc_21q1_skipkw.py`: `matches 20`; `bc_q_sheets.py`: `duplicates skipped 13 … images 282 tweets 262`.

### `bc_21q1_append.py` (перевірка перед записом)
```
BC-2021-01-18-SXP-LONG     t0 22:35:47 call_moment same_day ok=True group=in_profit own=strict scalp=False excl=- border=False q2=False
BC-2021-01-19-ETH-SHORT    t0 18:54:52 call_moment same_day ok=True group=in_profit own=strict scalp=False excl=- border=False q2=False
BC-2021-01-20-AKRO-LONG    t0 11:15:28 call_moment same_day ok=True group=ex_ante own=strict scalp=True excl=- border=True q2=True
BC-2021-01-20-ADA-LONG     t0 14:29:41 call_moment same_day ok=True group=ex_ante own=strict scalp=True excl=- border=True q2=True
BC-2021-01-20-CHZ-LONG     t0 14:29:41 call_moment same_day ok=True group=ex_ante own=strict scalp=True excl=- border=True q2=True
BC-2021-01-20-LTC-LONG     t0 14:29:41 call_moment same_day ok=True group=ex_ante own=strict scalp=True excl=- border=True q2=True
BC-2021-01-20-DOT-LONG     t0 19:12:06 call_moment same_day ok=True group=in_profit own=strict scalp=False excl=- border=False q2=False
BC-2021-01-21-BTC-SHORT    t0 17:16:09 call_moment same_day ok=True group=in_profit own=strict scalp=False excl=- border=False q2=False
BC-2021-01-25-BTC-LONG     t0 19:58:44 call_moment same_day ok=True group=ex_ante own=strict scalp=True excl=- border=True q2=True
BC-2021-01-26-SXP-LONG     t0 06:08:11 call_moment same_day ok=True group=in_profit own=strict scalp=False excl=- border=False q2=False
BC-2021-01-31-SXP-LONG     t0 15:52:23 call_moment same_day ok=True group=ex_ante own=strict scalp=True excl=- border=True q2=True
BC-2021-02-19-DOGE-LONG    t0 12:34:24 call_moment same_day ok=True group=ex_ante own=none scalp=True excl=- border=True q2=True
BC-2021-02-22-BTC-SHORT    t0 08:18:21 call_moment same_day ok=True group=in_profit own=strict scalp=False excl=- border=False q2=False
BC-2021-02-22-BTC-LONG     t0 12:29:09 call_moment same_day ok=True group=ex_ante own=strict scalp=False excl=- border=True q2=True
BC-2021-02-22-BADGER-LONG  t0 21:59:40 call_moment same_day ok=True group=ex_ante own=strict scalp=True excl=- border=True q2=True
BC-2021-02-26-COTI-LONG    t0 11:12:17 call_moment same_day ok=True group=ex_ante own=intent scalp=True excl=conditional border=False q2=False
BC-2021-02-26-DOT-LONG     t0 15:16:07 call_moment same_day ok=True group=ex_ante own=intent scalp=False excl=- border=True q2=False
BC-2021-03-02-CHR-LONG     t0 20:13:17 call_moment same_day ok=True group=ex_ante own=intent scalp=False excl=recovered border=True q2=True
parse_calls new rows 18 errors []
written 18 rows
```
### `bc_21q1_fixcheck.py` (після правки за рев'ю, скорочено: лише нові рядки й підсумок)
```
parse_calls all rows 216 errors []
BC-2021-01-27-GME-LONG     ok=True same_day group=unknown own=strict scalp=False excl=tradfi border=True alt='показано вже в плюсі)' primary=False
BC-2021-03-07-ETH-LONG     ok=True same_day group=unknown own=strict scalp=False excl=- border=True alt='(а), не рядок)' primary=False
BC-2021-03-15-HTR-LONG     ok=True same_day group=in_profit own=strict scalp=False excl=- border=False alt='' primary=False
rows 21 {'in_profit': 7, 'border': 13, 'q2': 10, 'long': 18, 'sym_empty': 21, 'short': 3, 'ex_ante': 12, 'unknown': 2} tweets 19
```
Решта 18 рядків — `ok=True`, мітки ті самі, що вище.

Стадія 0 без свічок (`scripts/verify_btc_charlie_eval.py --stage zero`, 24.11, після правки за рев'ю): `rows 216; refused / untagged 0`, `rows outside both samples (not evaluated): 38`, `sample main: 101 rows`, `primary 64`, `ISO weeks with a primary row (W): 27` — `main` і `combo` без змін. Тести після правки: `uv run pytest -q tests/test_call_labels.py tests/test_call_eval.py tests/test_trader_eval.py` — 77 passed; `make lint` чисто.
