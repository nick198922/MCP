# 2026-11-17 · Заклики @Trader_XO, 2022-Q4 (партія пункту 2; квартал неповний)

Це партія пункту 2 картки [IDEA-20261113-x-traders](../ideas/IDEA-20261113-x-traders.md): жовтень–грудень 2022 року. Текст і картинки переглянуто в одному проході. Метод, формати й критерії взято з [2022-Q3](2026-11-17-x-calls-trader_xo-2022q3.md) після раунду виправлень (разом із правилами Q2, Q1, 2021-Q4 і раніших партій):
- розкриття (відкрита позиція з ціною) дає рядок; закриті угоди й позиції, показані лише після цілі чи закриття (зокрема часткового), йдуть у (а);
- картка відкритої позиції з мовою результату, але без явного закриття, — розкриття, межовий (ATOM 27.08);
- картка у відповіді іншим — розкриття, межовий (TRX 14.06); картка без слів про вхід — розкриття, межовий (BTC 32 095.95);
- бажання з числом — умовний межовий рядок, лише якщо сторону видно на дату твіту;
- вхід на непідписаній лінії або в боксі без числа — (б); «N's» = N…N.99; ціль — підписаний рівень, який називає текст;
- план + виконання — один рядок (виконання лише в `note`).

Рядки дописано блоком «2022-Q4» у кінець [`docs/calls/x/Trader_XO.csv`](../calls/x/Trader_XO.csv) з рядком-коментарем `# 2022-Q4, НЕПОВНИЙ (…)`. Результати закликів не оцінювали, це пункт 3. Рішення «рядок / не рядок» ухвалено лише за текстом і картинкою твіту, без погляду на подальшу ціну. Пізніші твіти автора використано тільки для приміток (виконання, вихід). Тексти твітів — недовірені дані.

## Дані

- Архів `docs/library/courses/x-Trader_XO/` (недовірені дані). `tweets.json`: sha256 `06f1e9450086`, 20 590 твітів. `state.json`: sha256 `1da2f6affaf9`, `done_months` 2018-01 … 2026-10. Місяці `2022-10`, `2022-11`, `2022-12` є в `done_months`.
- **Квартал неповний: 71 порожній день з 92.** В архіві є лише 28–31.10, 28–30.11, 16.12, 18.12 і 20–31.12 (21 день).
  - Розрив 30.09 23:10 → 28.10 22:17 (01–27.10 порожні).
  - Розрив 31.10 23:14 → 28.11 17:03 (666 год; 01–27.11 порожні): **увесь обвал FTX (08–11.11) поза архівом.**
  - Розрив 30.11 21:35 → 16.12 10:20 (373 год; 01–15.12 порожні).
  - Дрібні: 16.12 23:51 → 18.12 12:38 (37 год), 18.12 12:38 → 20.12 03:51 (39 год).
  - Розриви — пропуск збору, а не пауза автора: з цитат відомі 10 власних твітів кварталу, яких нема в архіві (листопад 5, грудень 5), серед них два плани угод (ES 01.12, BTC 14.12) і виконання GBPUSD (17.12). Розрив жовтня збігається з тим, що 31.10 автор пише «Been a solid last 10 days into month end», — угоди 18–27.10 поза архівом.
- Склад: 440 твітів (жовтень 100, листопад 100, грудень 240), з них 283 відповіді іншим, 21 самовідповідь, 23 ретвіти. Власних твітів і самовідповідей без ретвітів — 134 (кандидатів фільтра 23).
- Фото: 100 фото-твітів, 113 картинок. Ще 10 картинок `quoted.media` 6 відновлених твітів завантажено через `Http` у `~/.cache/tradedesk-agents/x-calls-trader_xo-2022q4/qmedia/`.
- Інструменти лежать поза git, у `~/.cache/tradedesk-agents/tmp/`: `xo_22q4_dump.py` (перевірка `done_months`, розриви, відновлені цитати, завантаження `quoted.media`), `xo_22q4_sheets.py` (аркуші), `xo_22q4_classes.py` (рядки, класи, фільтри, `--apply` дописує CSV), `xo_22q4_csvcheck.py`. Аркуші — `~/.cache/tradedesk-agents/x-xo-22q4-sheets/`.

## Критерії

Діють критерії Q3 (перелічено вгорі). Межові застосування цього кварталу, кожне з прецедентом:
- **Картки з мовою результату без явного закриття** — розкриття, межові (ATOM 27.08 у Q3): DOGE 0.1496 (29.10, «Caught a good slice down»; через 14 хв у відповіді іншим «I’m done ✅ Just a day trade» — це вже після твіту з карткою, тож лише `note`), DOGE 0.1407 (30.10, «2 shorts back to back this weekend… Thanks for attending the casino»).
- **Картка у відповіді іншим** (TRX у Q2): ETH 1 627.65 (30.10, «@marketsmeowmeow Nice entry»). Вхід картки збігається з 31.10 «Locked in profits on … ETH 1620's», тож це позиція автора.
- **Картка без слів про вхід** (BTC 32 095.95 у Q2): BTC 20 905.54 (31.10 14:13, «🖕»), закрито того ж дня о 18:55.
- **Бажання з числом, сторону названо, у відповіді іншим**: GBPUSD 29.11 «I'd like to see British Rupee trade 1.22 - 1.23's… Would smack swing short back down» (TradFi); відновлений BTC 14.12 «I'd like to see btc clean up around 18.2 - 18.5 … watch them unwind back inside range» (сторону видно на графіку того дня: стрілка вниз від «TOP LIQ 18.5»).
- **План + виконання — один рядок** (Q1 / Q2): GBPUSD 29.11 → 17.12 «Caught 1.24 … closed out at 1.216’s», лінія «Entry» 1.23630 у зоні; BTC 14.12 → 16.12 картка 18 270.00 у зоні 18.2–18.5. Виконання і вихід — лише в `note`.
- **Нове межове застосування (виправлено за рев'ю): актив не названо, але його однозначно видно з історії ціни на графіку, а не з одного рівня, — умовний межовий рядок.** 30.12 14:15, відповідь @trader1sz без тексту: графік із підписаними лініями 1.23849 / 1.22837, синя стрілка вниз на 1.22837, поточна 1.20750 → `12-30-GBPUSD-SHORT`. Актив — з історії ціни на самому графіку: котирування в USD, п'ять знаків; з липня видно обвал кінця вересня 2022 нижче 1.08 і максимум середини грудня ≈ 1.245 (збігається з власним GBPUSD 1W автора 17.12, лінія 1.24810); той самий шаблон TradingView, рисунок рівнів зі стрілкою вниз і той самий співрозмовник, що в GBPUSD 1D 29.11 (`11-29-GBPUSD-SHORT`). EURUSD і крипта такої історії не мають. Однієї ціни 1.2075 для цього не досить (крос-курси, альткоїни біля $1.2), тому «.227» у Q3 ((а): лише рівень, без історії, що визначала б актив) лишається (а). Першу редакцію («обидві пари автор того дня обговорює») знято: 30.12 автор FX-пар не називає, «eurusd and gbpusd» — у твіті 29.11 11:08. Варіант «без цього рядка» — 9 рядків; у пункті 3 рядок і так TradFi.
- **Ціль «low N» — ближній до входу край діапазону** (прецедент `XO-2022-03-20-RUNE-SHORT`: «low 7's» → 7.3, наше прочитання 7.0–7.3). DOGE 31.10 «Back to low 11» (центи) → ціль **0.113** (наше прочитання 0.110–0.113; виправлено за рев'ю, перша редакція 0.11 брала дальній край і суперечила RUNE). Синя 0.08310 на графіку текст не називає (08:12 «towards blue level» — інший твіт без входу, (б)) — лише варіант у `note`.
- **Позиції, показані лише після руху, — (а)**: «Did you short complacency anon» (30.10, рівні DOGE після падіння), «SPX/ES_f paying out beautifully» (29.11, вхід не показано), «I closed my short last night… eurusd» (29.11), «$GBPUSD ✅» (20.12, рівень 1.22185 уперше після руху), ретроспективи AVAX (24.12) і «1 Year later» (29.12).
- **Лінії й бокси без підписів — (б)**: LINK «zone below over the next 3-6 months» (сірий бокс без чисел), ALTPERP «Give me the highs and we can ride it to the lows» (сині лінії без підписів), DOGE «towards blue level» (ціль 0.0831 без входу).

## Підсумок

**10 рядків з 10 твітів** (5 власних твітів в архіві, 3 відповіді іншим, 2 відновлені з цитат):

| id | Вид | Вхід | Ціль / стоп | Примітка |
|---|---|---|---|---|
| `10-29-BTC-SHORT` | розкриття | 21020.25 (картка) | — | показано в плюсі (ROI 13.69 %); Current 20 730.50; тригер — на стрімі 28.10; закрито 31.10 («BTC from 20.9») |
| `10-29-DOGE-SHORT` | розкриття, межовий | 0.1496 (картка) | — | показано в плюсі (ROI 153.35 %); Current 0.1265; «I’m done ✅» через 14 хв |
| `10-30-DOGE-SHORT` | розкриття, межовий | 0.1407 (картка) | — | показано в плюсі (ROI 167.31 %); Current 0.1170 |
| `10-30-ETH-SHORT` | розкриття, межовий | 1627.65 (картка) | — | показано в плюсі (ROI 18.76 %); відповідь іншим; закрито 31.10 |
| `10-31-DOGE-SHORT` | умовний | 0.13–0.135 | ціль 0.113 («low 11») | «rejection around 13 - 13.5 swing. Back to low 11»; 18:55 «probs wont pull trigger» |
| `10-31-BTC-SHORT` | розкриття, межовий | 20905.54 (картка) | — | показано в плюсі (ROI 27.68 %); без слів; закрито того ж дня |
| `11-29-GBPUSD-SHORT` | умовний, межовий, TradFi | 1.22–1.2399 | — | відповідь іншим; виконання 1.2363 (17.12, з цитати), вихід 1.216's |
| `12-01-ES-SHORT` | умовний, **відновлений**, TradFi | 4150–4159.99 | — | відомий лише з «✅ Thanks» 16.12 |
| `12-14-BTC-SHORT` | умовний, межовий, **відновлений** | 18200–18500 | — | відповідь іншим; виконання — картка 18 270.00 (16.12), «Trade closed» |
| `12-30-GBPUSD-SHORT` | умовний, межовий, TradFi | 1.22837 | — | актив не названо, GBPUSD з історії ціни на графіку (USD; обвал 09.2022 нижче 1.08, максимум 12.2022 ≈ 1.245; шаблон і співрозмовник 29.11); поточна 1.20750; варіант зони до 1.23849 |

Разом: **усі 10 — шорти**; жовтень 6, листопад 1, грудень 3. BTC 3, DOGE 3, GBPUSD 2, ETH 1, ES 1. Розкриттів 5 (усі **показано вже в плюсі**), умовних 5, межових 7, відновлених 2, TradFi 3 (`symbol` порожній). **Цілей 1 (DOGE 0.113), стопів 0.** Пробійних, спотових, хеджів нема. Видимих збитків автора в архіві нема: виходи, які видно (BTC і ETH 31.10 «Locked in profits», GBPUSD 1.216's, BTC 16.12 «Trade closed», ES «✅»), — у плюсі; ціни виходу названо лише для GBPUSD.

**Не-рядки.** Поіменно класифіковано 177 твітів: усі 134 власні твіти й самовідповіді, 33 відповіді іншим (26 збігів фільтра відповідей Q3 + 7 з проходу по картинках і контексту; виправлено за рев'ю, перша редакція «29 + 4» неправильно ділила ті самі 33) і 10 відновлених. Підсумок: R 10 · C 9 · (а) 7 · (б) 8 · (в) 143; **розмитих 158: (а) 7 · (б) 8 · (в) 143; продовжень 9**. Причина (в) без окремого запису — початок тексту твіту («text: …») або «image / link only»: макро, цитати, мемчики, футбол, трансляції, торговий щоденник.

### Пари й групи (для пункту 3)
- Окремі, але корельовані (CI кластером за тижнем): `10-29-BTC-SHORT` ↔ `10-31-BTC-SHORT` (21 020.25 і 20 905.54; 31.10 «Locked in profits on BTC from 20.9» — та сама позиція з добиранням або новий вхід, закриття одне); `10-29-DOGE-SHORT` ↔ `10-30-DOGE-SHORT` («2 shorts back to back this weekend») ↔ `10-31-DOGE-SHORT` (новий план); `10-29-BTC-SHORT` ↔ `10-30-ETH-SHORT` (той самий вікенд, закриті одним твітом 31.10); `11-29-GBPUSD-SHORT` ↔ `12-30-GBPUSD-SHORT`.
- План + виконання — один рядок (виконання лише в `note`): `11-29-GBPUSD-SHORT` (Entry 1.23630 17.12, вихід 1.216's), `12-14-BTC-SHORT` (картка 18 270.00 16.12).
- Відновлені — виключати за id (2): `12-01-ES-SHORT`, `12-14-BTC-SHORT` (обидва відомі лише з твітів-результатів «✅», відбір на виграш); виконання `11-29-GBPUSD-SHORT` теж відоме лише з цитати 20.12 «✅», сам план — в архіві.
- Межові (7) показувати окремо як чутливість: `10-29-DOGE-SHORT`, `10-30-DOGE-SHORT`, `10-30-ETH-SHORT`, `10-31-BTC-SHORT`, `11-29-GBPUSD-SHORT`, `12-14-BTC-SHORT`, `12-30-GBPUSD-SHORT`.
- Розкриття, показані вже в плюсі (5 з 5), оцінювати від ціни на дату твіту (Current картки): `10-29-BTC` 20 730.50, `10-29-DOGE` 0.1265, `10-30-DOGE` 0.1170, `10-30-ETH` 1 596.90, `10-31-BTC` 20 323.00. Обидва DOGE закрито / імовірно закрито в день показу — горизонтні метрики для них майже порожні.
- `10-31-DOGE-SHORT`: автор 18:55 «probs wont pull trigger here» — імовірно не виконано (скасування не явне); у пункті 3 — як звичайний умовний, з приміткою.
- TradFi (`symbol` порожній, `calls_check.py` ринок не знайде): `11-29-GBPUSD-SHORT`, `12-01-ES-SHORT`, `12-30-GBPUSD-SHORT`.
- Без стопу всі 10, з ціллю 1: метрика «середній R на заклик з відомим стопом» цього кварталу не торкнеться.

## Прохід по картинках

Переглянуто всі 113 картинок 100 фото-твітів і 10 `quoted.media`, разом 123:
- **14 твітів з тікером, карткою або планом (17 картинок)** — аркуші 2×2 по 800 px (`key-01…05.jpg`).
- **19 твітів другого ряду (25 картинок; трансляції, графіки без входу, відновлені)** — аркуші 3×3 по 600 px (`sec-01…03.jpg`).
- **Решта 81 картинка** — мініатюри 360 px по 20 на аркуш (`rest-01…05.jpg`), перевірені на картки позицій, лінії позицій і скриншоти бірж. Звідси прийшли два рядки: картка ETH 1 627.65 у відповіді іншим (30.10) і картка BTC 20 905.54 з текстом «🖕» (31.10); обидві фільтр за текстом не бачить.
- **Збільшення** (`zoom-01…02.jpg`, `crop-01.jpg`): DOGE 1H 30.10 (0.14344 / 0.13047 / 0.11589), графік 30.12 (заголовок без тікера; осі 1.23849 / 1.22837 / 1.20750), картка ETH 30.10.

Що дали картинки: усі 5 входів розкриттів (картки), виконання двох планів (Entry 1.23630, картка 18 270.00), рядок `12-30-GBPUSD-SHORT` (лише графік) і рішення (б) для LINK, ALTPERP і DOGE «blue level».

## Застереження

- **Квартал неповний: 71 з 92 днів без твітів** (є лише 21 день). Нема 01–27.10, 01–27.11 (увесь обвал FTX) і 01–15.12. Угоди 18–27.10 («solid last 10 days») відкрито й, імовірно, частково закрито в розриві. 10 власних твітів відомі лише з цитат. Перед пунктом 3 ці дні варто дозавантажити (`x_run.sh`), як і решту обрізаних місяців 2020–2024.
- **Відбір на виграш:** 5 з 5 розкриттів показано вже в плюсі (DOGE з ROI 153 % і 167 %); обидва відновлені плани відомі лише з твітів-результатів «✅»; виконання GBPUSD — теж. Умовних з архіву лише 3 (`10-31-DOGE`, `11-29-GBPUSD`, `12-30-GBPUSD`), з них два — TradFi.
- **Перекіс у шорти:** 10 з 10. Автор сам: 24.12 «there's really not much left for me to short in shitcoin land heading into 2023», 28.12 «Not bought anything Q4», 29.12 «2022 Trading performance > 2021 PnL» (самозвіт, неперевірний).
- **Перехід на FX:** з кінця листопада автор пише, що «Been playing eurusd and gbpusd» і «I dont tweet much about FX - normally cover it on my livestreams» (29.11), «day trading FX much more next year» (30.11). Отже, частина угод кварталу (EURUSD-шорт, закритий 28.11; GBPUSD-шорт з 1.24) була на трансляціях, а в X — лише після результату. Плани трансляцій (28.10, 23.12, 29.12) поза архівом.
- **Цілей 1, стопів 0.**
- Відповіді іншим: класифіковано 26 збігів фільтра Q3 і 7 твітів з проходу по картинках і контексту (разом 33; `EXTRA_REPLIES` має 8 id, один з них уже серед збігів фільтра). Решта ≈ 250 відповідей без слова входу чи плану й цифри — розмова, поіменно не переглядалися; картки у відповідях закрито проходом по всіх картинках.

## Списки рішень

Фільтр і класи — як у Q3 (фільтр відповідей — розширений, зі словами `ask|fill|handle|tag|clos|took|N's`). Кандидати фільтра — власні твіти й самовідповіді без ретвітів зі словами long / short / buy / sell / target / invalidation / stop / entry / tp / sl / bid / accumulate / load або з тікером і фото. Класи: **R** — рядок (або твіт, що несе рядок); **C** — продовження рядка; **(а)** — угода чи позиція без ціни входу або показана лише після руху, закрита угода; **(б)** — план зі стороною, але рівень без числа або без входу; **(в)** — погляд, рівень без сторони, жарти, трансляції. Дата — день твіту (UTC, з id). Причини англійською, як у `xo_22q4_classes.py`.

**Кандидати фільтра (23)** — R 4 · C 2 · (а) 3 · (б) 4 · (в) 10
- **R — рядки або носії рядків, 4:** `1586318677825261573` (29.10; 10-29-BTC-SHORT card entry 21020.25); `1586427153696329728` (29.10; 10-29-DOGE-SHORT card entry 0.1496 (borderline)); `1586742189740146689` (30.10; 10-30-DOGE-SHORT card entry 0.1407 (borderline)); `1587013643408924673` (31.10; 10-31-DOGE-SHORT rejection 13-13.5, back to low 11).
- **C — продовження рядка, 2:** `1603696495261372416` (16.12; BTC trade closed, card 18270 (12-14-BTC-SHORT execution)); `1603894001454780417` (16.12; ES ✅ thanks (12-01-ES-SHORT)).
- **(а), 3:** `1586723850695413761` (30.10; did you short complacency: DOGE levels shown after the move); `1605077180622462977` (20.12; GBPUSD ✅: spike-sell level 1.22185 shown after the move); `1606711116083728391` (24.12; AVAX never in doubt: retrospective of 03.2022).
- **(б), 4:** `1586994475016110080` (31.10; DOGE keep smacking shorts towards blue 0.0831: no entry level); `1587144854685028353` (31.10; BTC defensive about when to buy in: no level); `1587218549034057728` (31.10; LINK zone below for next cycle: box without numbers); `1597636161409028096` (29.11; ES chart (swing high, range high/lows) quoting 10.11: no entry).
- **(в), 10:** `1597392852790624256` (29.11; text: Trading - as in playing the shorter sub cycles within a bigg); `1597556069601247233` (29.11; AAPL $100 next year (forecast)); `1597601655612387328` (29.11; my 0.1btc sell order (joke)); `1598028217050230784` (30.11; text: JP: 'It is likely that restoring price stability will requir); `1603900775897366529` (16.12; BTC lows taken, won't be playing it); `1605982208602169344` (22.12; text: Everyone and their uncle and dog are short equities and bond); `1606714423732731906` (24.12; not much left to short (view)); `1607773620599934980` (27.12; TSLA monthly level (view)); `1608219428981821440` (28.12; AAPL flood gates (forecast)); `1608487566780637185` (29.12; BTC bounce over many months? (view)).

**Поза фільтром (111)** — R 1 · C 3 · (а) 3 · (б) 1 · (в) 103
- **R — рядки або носії рядків, 1:** `1587085467387830273` (31.10; 10-31-BTC-SHORT card entry 20905.54 (borderline, no words)).
- **C — продовження рядка, 3:** `1586354166166736897` (29.10; reasoning on stream, weekend trade (10-29-BTC-SHORT)); `1587124257238171653` (31.10; calling it a day, quote of 09:28 (10-31-DOGE-SHORT)); `1587156401243275265` (31.10; closed all trades: BTC from 20.9, ETH 1620's, DOGE probably no trigger).
- **(а), 3:** `1597630672428224512` (29.11; SPX/ES paying out: open short, entry not shown); `1606708825180016640` (24.12; ✅ chart, asset and entry not shown); `1608252708527509504` (29.12; 1 year later: retrospective of 12.2021).
- **(б), 1:** `1586373087297437696` (29.10; Nov monthly open as pivot for triggers: no side, no number).
- **(в), 103:** `1586119917690007552` (28.10; image / link only); `1586305896627347457` (29.10; text: 🔫); `1586372278472019968` (29.10; text: Q) So what is it that you do as an established trader? A) Tr); `1586441499998584833` (29.10; text: @trader1sz); `1586455818605457408` (29.10; text: 🤝); `1586479668810715138` (29.10; DOGE LTF market-cycle cheat sheet); `1586674476208586752` (30.10; text: 😂); `1586713337429204992` (30.10; text: Some Sunday Quotes: [L]); `1586718210921009156` (30.10; text: @Pentosh1 Sauce: [L]); `1586802896041312256` (30.10; text: No livestream tonight. Will be on Monday 31st around 2pm utc); `1586847377322905604` (30.10; image / link only); `1586978737253400576` (31.10; text: 👀); `1587067823184035841` (31.10; text: UK Mortgages: [L]); `1587087610027065350` (31.10; plans shared on Friday stream (outside archive)); `1587095266527793153` (31.10; text: Been a solid last 10 days into month end. Let’s spin up some); `1587215167217172481` (31.10; text: #FF @trader1sz & @crypto_iso Going to enjoy trading the rema); `1597312824883449856` (28.11; text: @trader1sz 😂); `1597322796715368450` (28.11; text: 'What the wise man does in the beginning, the fool does in t); `1597323723358449665` (28.11; text: Howard Marks - Mastering the Market Cycle.); `1597362989661065217` (28.11; text: It’s that time of the year again. I enjoy the process of rev); `1597384369496547329` (29.11; text: Fascinating listen [L]); `1597387645780623362` (29.11; text: @Crypto_Chase It was the exact reasons I went after all the ); `1597391004708982784` (29.11; text: Personal opinion Investing over the next several years will ); `1597392965269266432` (29.11; text: @Captain_Kole1 And so the liquidity games continue); `1597403805511217152` (29.11; text: @WifeyAlpha Maybe a case for dynamic asset allocation …); `1597584306440134656` (29.11; text: @trader1sz Just completed my CFA level 3 course. Also manage); `1597584534945824768` (29.11; text: Fuck sake I forgot to include: Ex Goldman Sachs as kitchen p); `1597588456045449223` (29.11; text: @livenletlive999 Next week is generous); `1597698146326708224` (29.11; text: @trader1sz 🤝🏴󠁧󠁢󠁥󠁮󠁧󠁿); `1597698798847135745` (29.11; text: Football > American Football); `1597979170667397120` (30.11; text: @trader1sz funny how some of these CFA fund managers larp ab); `1597979883766173696` (30.11; text: @trader1sz Could have thrown a dart blind folded at any high); `1597982589511688193` (30.11; text: Chicago PMI - something I've discussed frequently on the liv); `1597985473208516608` (30.11; text: @trader1sz Might differ for each and every individual. But f); `1597986140203929601` (30.11; text: @trader1sz Coding productivity at work went to shit ngl. Had); `1597988096678989825` (30.11; text: @trader1sz Enjoyed Engineering Mathematics though. Think we ); `1597994734655791109` (30.11; text: Latest addition to the collection 👌 [L]); `1598000565078077440` (30.11; text: @trader1sz we should do another Boxing Day / New Years Day l); `1598019160407412737` (30.11; text: Genuinely looking forward to day trading FX much more next y); `1598028220322156544` (30.11; image / link only); `1598068245898334209` (30.11; text: Catch you in Dec Goodnight); `1603829267170721797` (16.12; text: 👀); `1605048319507664897` (20.12; image / link only); `1605211550410760192` (20.12; text: #FF @KAPOTHEGOAT1 One of the best alts player from an FA per); `1605288173684379656` (20.12; text: Solid checklist - take note ✏️); `1605569620252819457` (21.12; text: Great read. Im not a scalper but for those interested:); `1605627178686615552` (21.12; text: Give this a watch folks... 🤝); `1605938063544356865` (22.12; text: Aye so where’s Santa then ?); `1605956768311529474` (22.12; text: Livestream Friday 23rd Dec - 10pm UTC Mainly focusing on #Bi); `1605956980463456257` (22.12; text: Will kick off the trading manual series after Boxing day.); `1606311030052773891` (23.12; text: Fast-track to trading: follow the best & form a style that s); `1606313306612011013` (23.12; text: @Captain_Kole1 👑); `1606317723285438464` (23.12; text: @davidbelle_ Average [L]); `1606328047031853057` (23.12; text: @FynnJL @Kr3py @SilvXBT @insilicobunker Difficult to perciev); `1606456545746771969` (24.12; text: @davidbelle_ That’s the one); `1606595687851999232` (24.12; text: Thanks for tuning in last night. Covered my high time frame ); `1606627649253695488` (24.12; text: #FF @EPBResearch One of my favourites for macro this year. B); `1606714260284915717` (24.12; text: @PixaVs Nope Zhupercycle said even higher - they drank cool ); `1606977221553684488` (25.12; text: 🎄); `1607087705497931776` (25.12; image / link only); `1607109734930452480` (25.12; text: One of the best if not the best thread I've read this month ); `1607113291477680128` (25.12; text: 2023: Let the liquidity games continue); `1607301402870837248` (26.12; image / link only); `1607378844666707968` (26.12; text: @davidbelle_ Majority of onchain analysts); `1607546020375977985` (27.12; text: Before and After bridging to Polygon [L]); `1607706895317352448` (27.12; text: ⁦@trader1sz⁩ [L]); `1607763057777803264` (27.12; text: Still over valued imo [L]); `1607766634273333250` (27.12; image / link only); `1607835493227532288` (27.12; text: 🤝 [L]); `1607838627349860352` (27.12; text: Varane > Maguire And it’s not even close); `1607846902439247872` (27.12; text: Fitbit ?); `1607848788017963008` (27.12; text: Ok Garmin it is. Cheers ✌️); `1608099659464966145` (28.12; text: Not been reading much of late but no surprise another crypto); `1608101752036888576` (28.12; text: 85% [L]); `1608106864880893952` (28.12; text: Roger knows [L]); `1608142982628401154` (28.12; image / link only); `1608149909626130433` (28.12; text: Trading Discords / Telegrams etc... I know a strong trading ); `1608150136630149120` (28.12; text: There comes a point where you just follow your own system wh); `1608205058453262350` (28.12; SOL single digit (view)); `1608228606379020288` (28.12; text: Livestream Thursday 29th Dec - 1pm utc Primarily covering #b); `1608245182998134784` (28.12; not bought anything Q4); `1608441221625663489` (29.12; text: Live-streaming in 30 mins time); `1608466067898204161` (29.12; text: @trader1sz [L]); `1608517461569306625` (29.12; text: Lol); `1608533781228711938` (29.12; text: @LiveLikeIts1987 It also ran the first tbh.); `1608562415238778880` (29.12; text: Rest in Peace - 10 [L]); `1608607705404698624` (29.12; text: @trader1sz bear market twatter boring. Much more enjoyable s); `1608609030452432897` (29.12; text: @trader1sz Maybe FX based); `1608609520443588608` (29.12; text: @SCTrader4 @trader1sz Only when I cba); `1608610554884816896` (29.12; text: New Years SZ XO Livestream 11th Jan 2023 - 8PM UTC FX outloo); `1608643176155447296` (30.12; text: 2021-2022: Almost like trading the ultimate wave. The magnit); `1608757807993737217` (30.12; image / link only); `1608795206144647173` (30.12; text: Often get asked which trading books or courses to watch. Rea); `1608804370417876993` (30.12; text: Great thread and one of the best around.); `1608840072992018437` (30.12; text: Bitcoin & Alts market review from yesterday. Covered #bitcoi); `1608903396303728642` (30.12; text: Lost count the number of times a bullish narrative was pushe); `1608904064095621123` (30.12; text: Jump in folks.); `1608939327337418753` (30.12; text: Good takes 👍); `1608964720484298752` (30.12; text: Appreciate the support and love many have shown. Will be pus); `1609178079422828544` (31.12; text: @trader1sz Equally for women who are thick as fuck. If a man); `1609186129751810049` (31.12; text: @TommyAshby76 @trader1sz 👑); `1609286108076019713` (31.12; text: Looking forward to releasing my second article in the new ye); `1609303426298482690` (31.12; image / link only).

**Відповіді іншим (33)** — R 3 · C 3 · (а) 1 · (б) 3 · (в) 23
- **R — рядки або носії рядків, 3:** `1586657444322856961` (30.10; 10-30-ETH-SHORT card entry 1627.65 (borderline, reply)); `1597548954409136128` (29.11; 11-29-GBPUSD-SHORT 1.22-1.23's swing short (borderline, reply, TradFi)); `1608829127699857408` (30.12; 12-30-GBPUSD-SHORT chart 1.22837 (borderline, asset from chart price history, TradFi)).
- **C — продовження рядка, 3:** `1586351590709288960` (29.10; aggressive SFP entry, BTC 21019.06 (10-29-BTC-SHORT)); `1586430848756338692` (29.10; I'm done ✅ just a day trade (10-29-DOGE-SHORT)); `1586353401989943297` (29.10; watch the stream at 18 min (10-29-BTC-SHORT)).
- **(а), 1:** `1597537399684608001` (29.11; closed EURUSD short last night: entry not shown).
- **(б), 3:** `1586637689494945795` (30.10; that level up there for shorting: no number); `1608793685663285248` (30.12; dollar lower, shorts on fx pairs: no level); `1587221378633187328` (31.10; ALTPERP highs to lows: lines without labels).
- **(в), 23:** `1586716995965358080` (30.10; text: @Pentosh1 If you've got no interest in crypto innovation and); `1597588171721965568` (29.11; I closed first (banter)); `1597633452182237184` (29.11; macro accumulation before $589 (view)); `1597987818198167553` (30.11; text: @trader1sz Did 2 degrees Electronic Engineering Comp sci wit); `1606455079271374848` (24.12; text: @insilicobunker @azidynamics Makes 0 sense Never trust any c); `1606660217046982657` (24.12; text: @trader1sz @DaRealMilkBagz Fam which Kanpai Panda am I buyin); `1606661625410224129` (24.12; text: @DaRealMilkBagz @trader1sz @crypto_iso Got too caught up sho); `1606715297435623425` (24.12; text: @AlphaTrader15 Its pointless. Like longing at all time highs); `1607366576638160903` (26.12; text: @StrongHedge [L]); `1607380302732877825` (26.12; text: @trader1sz Yeah quite busy last two days tbh. Just about fin); `1607387462678216708` (26.12; 30 cents and I stop (joke)); `1607741590348390400` (27.12; text: @trader1sz @TheCryptoLes @ApeDurden @joshnomics @Captain_Kol); `1607771329939349504` (27.12; text: @btc_charlie Have modesty with the targets mate [L]); `1607774571838545923` (27.12; text: @fenwbst150 Well yes of course they are. Fundamentals are ke); `1607843711496462336` (27.12; text: @Krzysie87817053 @dylanleclair ask me next year); `1608191361781731328` (28.12; text: @btc_charlie I mean tbh its the perfect exit strategy. Partn); `1608244848288579586` (28.12; text: @high_fades @pierre_crypt0 @CryptoCred @LSDinmycoffee @bitco); `1608802347987390464` (30.12; text: @StrongHedge @trader1sz @KAPOTHEGOAT1 @ApeDurden @TheCryptoL); `1608863160848769025` (30.12; Hedged (elephant meme)); `1609311055368404994` (31.12; text: @KAPOTHEGOAT1 @bitcoin_brown @trader1sz @_RN03xx_ @TysonThe2); `1586378519881605120` (29.10; yep no setup - took off (context unknown)); `1607761403762380801` (27.12; liquidated on demo acc (joke)); `1607926016214802433` (28.12; 20x on xmas day only (joke)).

**Відновлені (10)** — R 2 · C 1 · (а) 0 · (б) 0 · (в) 7
- **R — рядки або носії рядків, 2:** `1598362686877798407` (01.12; 12-01-ES-SHORT 4150's (recovered, TradFi)); `1602827628842569729` (14.12; 12-14-BTC-SHORT 18.2-18.5 (recovered, borderline)).
- **C — продовження рядка, 1:** `1604220953822695432` (17.12; GBPUSD caught 1.24 closed 1.216's, Entry 1.2363 (11-29-GBPUSD-SHORT execution)).
- **(в), 7:** `1590827305466703872` (10.11; ES strong momentum, not eager to short (view)); `1590856595046473728` (10.11; SOL Part II who capitulated (view, charts without entry)); `1591479875927683072` (12.11; 2022 bullshit filter (no trade)); `1593398243634839553` (18.11; analysts vs traders (no trade)); `1597000502033534976` (27.11; short term up, long term down into 2023 Q1 (view)); `1600646718550577155` (08.12; create a trading manual (no trade)); `1603847549588119552` (16.12; late shorts into local range low, watch from the sidelines).

## Вивід перевірок

`uv run --project . python -I ~/.cache/tradedesk-agents/tmp/xo_22q4_dump.py`:
```
state sha256 1da2f6affaf9 months 2018-01 .. 2026-10 n 106
quarter months done: {'2022-10': True, '2022-11': True, '2022-12': True}
tweets.json sha256 06f1e9450086 n 20590 min 2020-06-11
by month 2022-08..2023-02: {'2022-08': 240, '2022-09': 200, '2022-10': 100, '2022-11': 100, '2022-12': 240, '2023-01': 260, '2023-02': 201}
last tweet before quarter 2022-09-30T23:10 first after 2023-01-24T12:24
tweets 440 {'reply_other': 283, 'rt': 23, 'self': 21} by month {'2022-10': 100, '2022-11': 100, '2022-12': 240}
first 2022-10-28T22:17 last 2022-12-31T23:22
days without tweets 71 [10-01 … 10-27, 11-01 … 11-27, 12-01 … 12-15, 12-17, 12-19]
gaps >24h ['10-31T23:14->11-28T17:03 666h', '11-30T21:35->12-16T10:20 373h', '12-16T23:51->12-18T12:38 37h', '12-18T12:38->12-20T03:51 39h']
missing own quoted 12 in quarter 10
missing own quoted in quarter by month {'2022-11': 5, '2022-12': 5}
photo tweets 100 photos 113 by month {'2022-10': 31, '2022-11': 17, '2022-12': 52}
media but no file 0 []
qmedia files 10
```

`uv run --no-project python -I ~/.cache/tradedesk-agents/tmp/xo_22q4_classes.py --apply`:
```
own+self 134 cand 23 replies 33
unknown CLS keys: []
groups {'Кандидати фільтра': 23, 'Поза фільтром': 111, 'Відповіді іншим': 33, 'Відновлені': 10} total 177 R 10 · C 9 · (а) 7 · (б) 8 · (в) 143
rows 10 Counter({'short': 10}) [('BTC', 3), ('DOGE', 3), ('GBPUSD', 2), ('ETH', 1), ('ES', 1)] bad []
dups with CSV []
appended 10
```

Раунд виправлень (17.11, після рев'ю): у скрипт додано друк розбивки відповідей; повторний запуск без `--apply` (CSV не змінює; рядки вже є, тому `dups` — усі 10):
```
reply filter matches 26 extra added 7
own+self 134 cand 23 replies 33
groups {'Кандидати фільтра': 23, 'Поза фільтром': 111, 'Відповіді іншим': 33, 'Відновлені': 10} total 177 R 10 · C 9 · (а) 7 · (б) 8 · (в) 143
```

`uv run --no-project python -I ~/.cache/tradedesk-agents/tmp/xo_22q4_csvcheck.py`:
```
12 columns, rows 457 bad [] dups []
q 10 Counter({'2022-10': 6, '2022-12': 3, '2022-11': 1}) Counter({'short': 10})
[('BTC', 3), ('DOGE', 3), ('GBPUSD', 2), ('ETH', 1), ('ES', 1)]
targets 1 stops 0 horizon 0
geometry bad []
dates sorted True
id prefix ok True
```
