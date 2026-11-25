# 2026-11-25 · Перевірка (а) паперового carry — ПРОМІЖНИЙ прогін (не вердикт)

> **ПРОМІЖНИЙ ПРОГІН. Це не вердикт (а).** Мета, за реєстрацією, — рано знайти баг рушія. Вердикт дає лише фінальний прогін `--as-of-ms 1795968000000` після 29.11 16:35 UTC тим самим кодом.
> Картка: [`IDEA-20261017-forward-paper-carry`](../ideas/IDEA-20261017-forward-paper-carry.md), розділ «Реєстрація перевірки (а)». Тут немає P&L і нічого, крім того, що друкує зареєстрований скрипт.

## Підсумок

| книга | (а.1) | (а.2) | стан (а) за скриптом | розбіжностей |
|---|---|---|---|---|
| A `cc-paper` | `pass`: рішення 314/314, облік 2 359/2 359, (а.1-F) 298/298, (а.1-P) 291/291 | `incomplete` (◯): рішення 314/314 (open 7/7, close 4/4, hold 303/303), класів 0; покриття `need` 6/92 | ◯ | 0 |
| B `pp-paper` | `pass`: рішення 553/553, облік 7 389/7 389, (а.1-F) 2 289/2 289, (а.1-P) 1 056/1 056 | `pass`: рішення 553/553 (1.0), open / close 41/41 (1.0), класів 0 | ✓ (проміжно) | 0 |

- **Бага рушія чи фіда не знайдено.** Жодної розбіжності (а.1) у жодній книзі, у (а.2) жодного рядка в жодному класі (`data_hold`, `engine_data_unavailable`, …, `unexplained`, `not_checked`), конкуренції за слот 0.
- **A ◯ — через заморожене правило покриття, а не через збій чи розбіжність** (див. «Знахідка 1»). На фінальному прогоні головний результат A за реєстрацією знову буде ◯; погоджено вторинний результат `need*` (крок (б) нижче).
- **Межа (а) для A вузька.** З 12.11 рушій A нових входів не ранжує (`entries_stopped`), тож 303 із 314 рішень — hold утримуваних позицій, а відкриття A (7) і закриття (4) перевірено на малому n; усі 7 відкриттів — до 12.11.
- **Знайдено баг раннера (не рушія):** колізія імені файлу кешу для двох символів з не-ASCII назвами (див. «Знахідка 2»). На рішення цього прогону не вплинула: за `cp_funding` максимальний APR обох символів (0.110 і 0.142) нижчий за поріг входу A 0.20 у всіх 107 циклах. Справжню історію `牛来` при цьому не перевірено.
- **Погоджені наступні кроки** (рев'ю [`docs/reviews/2026-11-25-carry-paper-replay-interim.md`](../reviews/2026-11-25-carry-paper-replay-interim.md), «Рішення координатора»; у цьому кроці не зроблено): (а) крок розробника з критиком C — безколізійний ключ кешу, `[]` не кешувати, атомарний запис, злиття в `main` до 29.11 16:35 UTC (знахідка 2); (б) крок кванта з критиком B — вторинний результат `need*` окремим комітом до фінального прогону (знахідка 1).

## Код

Скрипт друкує перші 16 знаків; повні sha256 файлів робочого дерева збігаються з `origin/main` і з комітом реєстрації `9168a553` (злитий у `e8aa3634`); `git diff 9168a553 origin/main` і `git diff origin/main` по п'яти шляхах порожні.

| файл | sha256 |
|---|---|
| `src/tradedesk/analytics/carry_replay.py` | `2089a30ce2577fe6ce1769382be66a8b28004663871780e97972252b436b90db` |
| `scripts/verify_carry_paper_replay.py` | `0278f398a4f6086181257b9cbbe76903fe7ccae1a8b675c370d6dbcde3197d21` |
| `src/tradedesk/analytics/carry.py` | `1176c8b7db3787a8a8cc30b017bd34f4dbcc447431c1e03c059bf005ee634057` |
| `src/tradedesk/analytics/carry_rules.py` | `4b04ac8c968cee32aa22106a6e96204e77da11c0cb5ff12363511dce9ddbb50c` |
| `src/tradedesk/collector/market_data.py` | `1ecbe14557ab7d69d845e9b00adeba3814b08526a5cebc879f73da59dc714371` |

Після прогону жоден файл не змінювався.

## Команди

```bash
uv run python scripts/verify_carry_paper_replay.py --stage a1 --as-of-ms 1795564800000
uv run python scripts/verify_carry_paper_replay.py --stage a2 --as-of-ms 1795564800000   # у фоні
```

- БД `/var/lib/tradedesk-mcp/tradedesk.sqlite`, `mode=ro`.
- Результати: `~/.cache/tradedesk-agents/carry_replay/a1-1795564800000.json`, `a2-1795564800000.json`; кеш завантажень — `carry_replay/data/`.

## Період і n

- Цикли — 20.10 16:00 … 25.11 00:00 UTC. У кожній книзі 107 циклів `done`, дірок сітки 0, пропущених / невдалих 0, `excluded` не обрізано (пропущених циклів (а.2) 0).
- Вікно завантаження (а.2): 2026-10-11 16:00 … 2026-11-25 01:05 UTC.
- Рішення (об'єднання ключів рушія й перерахунку):

| книга | open | close | hold | разом |
|---|---|---|---|---|
| A | 7 / 7 | 4 / 4 | 303 / 303 | 314 / 314 |
| B | 23 / 23 | 18 / 18 | 512 / 512 | 553 / 553 |

- Однакові числа в (а.1) і (а.2) (збіг / n).
- Git SHA з розбіжністю: 0 з 101 в обох книгах.
- Прапорець `entries_stopped`: A — у кожному циклі після 12.11 00:00 («циклів після дати зупинки без прапорця» 0); B — у жодному (інакше була б розбіжність (а.1)).
- Відкладені позиції (переліком, як зареєстровано):
  - A — 16: MITO 14 (23.10 16:00 … 30.10 00:00) і VELODROME 2 (28.10 16:00, 29.10 00:00), усі `funding_payment_missing`, `detail=scheduled_payment_missing`;
  - B — 2: IRYS 20.11 08:00 і 16:00, `detail=payment_gap`.
- `data_hold`, де бектест закрив би відкладену позицію стопом: 0 в обох книгах.

## (а.2): покриття і запобіжники

| | A | B |
|---|---|---|
| символів всесвіту (`syms`) | 494 | 461 |
| з funding | 494 | 458 |
| з барами | 8 (+ `XMR`: закешований порожній список, знахідка 1) | 324 |
| покриття `must` (утримувані + кандидати) | 5/5 | 303/303 |
| покриття `need` | **6/92** (< 0.95 → ◯) | 324/324 |
| funding всесвіту | 491/491 | 458/458 |
| не лістяться зараз (поза знаменниками) | MITO, PTB, VELODROME | MITO, PTB, VELODROME |
| запобіжник funding | за скриптом 1/491 = 0.0020 з відмінністю, не порівняно 0 → `ok`; по суті 1 нога (`牛来`, знахідка 2) **не порівняна**, а не «з відмінністю»: відмінностей 0 з 490 порівняних, стан той самий `ok` | 0/916; `binanceusdm` 0/458, `bybit` 0/458 → `ok` |
| запобіжник цін | 0/1 042, не порівняно 0, не лістяться 136 → `ok` | 0/5 618, не порівняно 0, не лістяться 16 → `ok` |
| таблиця цілісності `cp_funding` | бракує 0, зайвих 0, ставка інша 38 (усі в одній нозі `牛来/USDT:USDT`, яку через колізію порівняно з історією `币安人生`, знахідка 2) | 0 / 0 / 0 |

- У B три Bybit-завантаження не вдалися: MITO, PTB, VELODROME, `data_unavailable: bybit does not have market symbol`. Це делістинги, вони поза знаменниками.
- `incomplete` / `truncated` завантажень funding — 0.

## Знахідка 1: A (а.2) ◯ — правило `need` рахує символи, яких рушій A не міг розглядати

- **Факт.** Знаменник `need` A — 92 символи, що лістяться зараз (MITO, PTB, VELODROME поза ним); бари є для 6, бракує 86 (`coverage.need.lacking` в `a2-1795564800000.json`). З цих 86:
  - **85** — `fetch_bars` спотових барів повернув `data_unavailable: binance does not have market symbol <X>/USDT`. Серед них GOAT, PIPPIN, SONIC, H, M, IRYS, RATS, … (перелік у виводі нижче);
  - **XMR/USDT:USDT** — спотовий запит `XMR/USDT` повернув порожній список без `missing` і без `warnings`. `fetch_bars` закешував його як успіх (`data/binance_XMR_USDT_bars4h_1791734400000_1795568700000.json` = `[]`, єдиний порожній серед 9 файлів барів A), і символ відпав на `if perp and spot` без жодного рядка у виводі.
  - **PTB** теж повернув `data_unavailable` і стоїть серед 86 надрукованих рядків `unavailable`, але він делістингований і в знаменник не входить. Тому 86 надрукованих рядків — це 85 символів `lacking` плюс PTB, а не ті самі 86 символів.

  Покриття `need` 6/92 < 0.95, тому (а.2) A `incomplete`, а (а) A — ◯.
- **Корінь (перевірено read-only, БД `mode=ro`).**
  - Усі 86/86 символів `lacking` (разом з XMR) записані рушієм A в `excluded` з причиною `no_spot` у **кожному** зі 107 циклів `done`: 9 202 пари символ-цикл, інших причин немає. Жоден не був записаним кандидатом. Незалежна перевірка критика (`tmp/critic_interim_check2.py`, БД `mode=ro`) дає те саме.
  - У коді (а.2) всесвіт кроку — `(set(data.markets) | set(rec["entry"])) - excl - aside`, а при `entries_stopped` — `[]` (`carry_replay.py:997`), тобто записані `excluded` циклу прибрано. Дані символу, виключеного в кожному циклі, не входять у жоден крок, навіть якби бари були.
  - Фід A (`collector/carry_paper_feed.py:106–107`, `universe_a`) виключає перп без активного спотового `X/USDT` на Binance. Тому такі символи ніколи не входять у крок A, ні в рушії, ні в (а.2): всесвіт кроку (а.2) — «мінус записані `excluded` циклу».
  - У `need` вони потрапляють так. Раннер бере всесвіт A з `cp_funding` на `binanceusdm` (`_a2_book`, рядок 506), а ця таблиця спільна з книгою B: фід B зберігає funding усіх перпів Binance, і зі споту, і без нього. Далі `need` (рядки 529–538) додає кожен символ, у якого завантажений APR хоч раз ≥ порогу входу, і не віднімає записаних `excluded`. `listed_now` для A перевіряє лише перп-біржу (`_venues("A")`), тому ці символи не вважаються «не лістяться».
- **Чи це збій завантаження?** Ні. Для 85 символів спотового ринку на Binance зараз немає; для XMR свічок у вікні немає (порожня відповідь); рушій виключав усі 86 як `no_spot` у кожному циклі. Повтор нічого не дасть. Отже, і дозволена зміна `fetch_bars` «для надійності завантаження» тут не годиться: вона змінила б семантику (відсутні дані → «є дані»).
- **Побічна вада раннера (неблокуюча).** `fetch_bars` кешує порожню відповідь як успіх (XMR). На вердикт це не вплинуло: символ потрапляє в `lacking` і робить (а.2) ◯, а не мовчки «проходить». Але тимчасова порожня відповідь закешувалася б для цього вікна назавжди. Виправляє крок (а) нижче (знахідка 2).
- **Що (а.2) A 314/314 означає.** До 12.11 крок (а.2) A бачить лише символи з завантаженими барами (8) плюс записаних кандидатів, з 12.11 його всесвіт порожній (`entries_stopped`); решту всесвіту рушія крок не містить. Відкрити такі символи не могли б ні рушій, ні бектест лише тому, що їхній завантажений APR нижчий за поріг входу (на цьому тримається правило `need`); виняток — `牛来`, чий APR узято з чужої історії (знахідка 2). Крім того, з 12.11 A нових входів не ранжує: 303 з 314 рішень — hold, відкриття 7/7 і закриття 4/4, усі відкриття до 12.11 (останнє — 07.11 08:00). Для A стратегія вже `rejected` за (б), тож (а) A — лише коректність рушія, і вона перевірена вузько.
- **Погоджений наступний крок (б), у цьому кроці не зроблено.** Крок кванта, **окремим комітом до фінального прогону** (у `main` або в гілці фінального прогону раніше за будь-який `a1/a2-1795968000000.json`), рев'ю критика B (лише текст):
  - поправка реєстрації — новим підрозділом **після** розділу «Реєстрація перевірки (а)» картки; текст реєстрації не змінюється; формулювання — з рев'ю, «Рішення координатора → Знахідка 1»;
  - **вторинний** результат (обидві книги): `need*` = `need` мінус символи, які рушій записав в `excluded` (будь-яка причина) у **кожному** циклі `done` до `as_of` включно; правило покриття те саме (≥ 0.95, без символів, що не лістяться зараз); `must`, покриття funding всесвіту, класи, запобіжники, пороги й вердикт не змінюються; вторинний (а.2) = `pass` лише тоді, коли покриття `need` — єдина причина `incomplete` у головному, а `need*` ≥ 0.95;
  - рахує окремий скрипт без мережі з фінального `a2-1795968000000.json`, БД `mode=ro` і кешу фінального вікна; п'ять заморожених файлів не змінюються;
  - **головний результат фінального звіту лишається за комітом реєстрації** (для A очікувано ◯); вторинний подається поруч як exploratory («за поправкою після проміжного прогону»), ✗ не поліпшує і головного не замінює. Вужчі варіанти («лише цикли з APR ≥ порогу», «лише цикли без `entries_stopped`») наперед відкинуто.

## Знахідка 2: колізія імені файлу кешу раннера (не баг рушія чи фіда)

- **Факт.** У таблиці цілісності A одна нога з відмінністю: `binanceusdm 牛来/USDT:USDT`, 38 виплат зі «ставкою інша», бракує 0, зайвих 0.
- **Корінь.** `scripts/verify_carry_paper_replay.py:389–390`, `_cache_file`, будує ім'я файлу через `re.sub(r'[^A-Za-z0-9]+', '_', sym)`. Обидва символи з не-ASCII назвою в `cp_funding` — `币安人生/USDT:USDT` і `牛来/USDT:USDT` — дають той самий файл `binanceusdm__USDT_USDT_funding_1791734400000_1795568700000.json`. Інших колізій ключа серед символів `cp_funding` немає.
  - Першим завантажено `币安人生`. `fetch_funding` для `牛来` знайшов «свій» файл у кеші й прочитав історію `币安人生`.
  - Перевірка read-only: файл збігається з `cp_funding` `币安人生` у 184/184 спільних мітках часу, з `cp_funding` `牛来` — лише у 143/184.
- **Вплив.**
  - **Обидва символи — у всесвіті рушія A:** в `excluded` A їх немає в жодному зі 107 циклів (перевірено read-only по `cp_cycle.payload`), тобто активний спот для них у рушія є. У B вони 107/107 `no_second_venue` (їх нема на Bybit), тож B колізія не стосується.
  - **Членство `牛来` у `need` визначено за історією `币安人生`.** Якби справжній APR `牛来` хоч раз перетнув поріг входу, символ мав би потрапити в `need` і отримати бари; без барів його тихо немає у всесвіті кроку, і пропущене відкриття (а.2) було б непомітним.
  - **Чому на рішення не вплинуло** (`tmp/critic_interim_check4.py`, код раннера, лише читання; перезапущено мною, той самий результат): поріг A `entry_apr` = 0.20, lookback 168 год; за `cp_funding` максимальний APR `牛来` у 107 циклах — 0.110, `币安人生` — 0.142; за файлом із колізією перетинів порогу 0. Після 12.11 A нових входів не ранжує (`entries_stopped`). Тому жоден з двох символів не міг бути відкритий ні рушієм, ні бектестом, і розбіжностей (а.2) 0.
  - **Чого ця перевірка не робить.** Справжню історію `牛来` не завантажено, тож у (а.2) A цей символ по суті не перевірено. Запобіжник funding A друкує «1/491 з відмінністю, не порівняно 0»; правильно читати «**1 нога не порівняна**, відмінностей 0 з 490»; стан однаково `ok`.
- **Погоджений наступний крок (а), у цьому кроці не зроблено.** Окремий крок розробника з рев'ю **критика C**, злиття в `main` **до 2026-11-29 16:35 UTC** (не в цій гілці: тут код дорівнює реєстрації):
  - безколізійний ключ кешу **лише всередині `fetch_funding` і `fetch_bars`** (наприклад, одразу після `p = _cache_file(...)` додати до імені суфікс sha1 символу); сам `_cache_file` не змінюється, бо його не названо серед дозволених частин;
  - порожній `[]` вважати недоступним і не кешувати (вада XMR, знахідка 1);
  - атомарний запис файлу кешу (тимчасовий файл + `replace`), щоб обірваний процес не лишав битий JSON;
  - правило `complete is False` / `dropped_rows > 0` → `None` у `fetch_funding` не змінюється;
  - тести, що падають на старому коді: два не-ASCII символи з різними рядками (окремо для `fetch_funding` і `fetch_bars`) мають повертати кожен свої дані; порожні бари не кешуються.
  - Фінальний звіт показує `git diff 9168a553 -- scripts/verify_carry_paper_replay.py` (зміни лише у `fetch_funding` / `fetch_bars`) і що sha256 чотирьох інших файлів дорівнюють `9168a553`. Якщо крок не встигне до 29.11 16:35 UTC, фінальний прогін іде кодом реєстрації, а фінальний звіт повторює застереження цієї знахідки.
  - Кеш проміжного прогону фінальний прогін не перевикористає: `until` у імені файлу інший, а після зміни ключа імена змінюються для всіх символів.

## Час і ліміти

- (а.1): ≈ 4 с на обидві книги, RSS 88 МБ.
- (а.2): 19:32 … 20:13 UTC, ≈ 41 хв. A — 1 604 с (funding Binance ≈ 3 с/символ), B — 840 с (Binance з кешу A, Bybit і бари). Пік RSS 701 МБ.
- Тихе вікно 00 / 08 / 16 UTC прогін не перетнув.
- Подій `rate_limited` / `truncated` 0, `(a.2) stopped: None`.

## Застереження

- Проміжні числа. Фінальний прогін додасть цикли 25.11 08:00 … 29.11 16:00.
- Межі (а), як у реєстрації: правильність **відкладання** і фільтри всесвіту (`excluded`) не перевіряються, а помилку всередині спільних функцій правил (а) не впіймає.
- Застереження скрипта: у варіанті «з funding рушія» символ без записаного кандидата бере APR з `cp_funding` у нинішньому стані (`INSERT OR REPLACE`). Оскільки розбіжностей немає, цей варіант не застосовувався.

## Сирий вивід (скорочено: без рядків прогресу, 86 однакових рядків `bars binance <X>/USDT: unavailable` і 16 рядків `aside` A, перелічених вище)

Примітка: 86 рядків `unavailable` — не ті самі 86 символів `need.lacking`. Це 85 символів `lacking` плюс PTB (делістинг, поза знаменником). 86-й символ `lacking`, XMR, рядка не має: його спотовий запит повернув `[]`, і `fetch_bars` закешував це як успіх (знахідка 1).

```
A: replayed {'done': 107} in 0.8 s, rss 178 MB
B: replayed {'done': 107} in 1.2 s, rss 209 MB
A: 494 symbols, window 2026-10-11 16:00 .. 2026-11-25 01:05, listed now 817, rss 209 MB
  bars binance RATS/USDT: unavailable or incomplete (['data_unavailable: binance does not have market symbol RATS/USDT']), not cached
  ... (ще 85: 4, ACU, AIO, AKE, AKT, ALCH, APR, ARC, ARIA, ARX, AVAAI, B2, BAN, BEAT, BILL, BLESS, BLUAI, BSB, BTR, CAP, CC,
       CLANKER, CLO, CT, CTR, CYS, DOS, EDGE, ELSA, ESPORTS, EVAA, FHE, FIGHT, FLOCK, FLUID, FOLKS, GOAT, GRIFFAIN, GRVT, GWEI,
       H, HANA, IDOL, IN, INX, IRYS, JCT, JELLYJELLY, KGEN, LAB, LIGHT, LYN, M, MAGMA, MAVIA, NAORIS, O, ON, PHAROS, PIEVERSE,
       PIPPIN, PONS, POWER, PTB, Q, RAVE, RIVER, SIREN, SONIC, SOON, SPACE, SQD, STABLE, TA, TAC, TRIA, TRUST, TRUTH, US,
       VELVET, XAN, XNY, XPIN, ZEREBRO, ZEST — /USDT на binance spot)
  guards: funding legs 1/491 differ (not compared 0), by venue binanceusdm 1/491 = 0.002036659877800407 (not compared 0) -> ok -> ok; prices 0/1042 differ (not compared 0) -> ok
B: 461 symbols, window 2026-10-11 16:00 .. 2026-11-25 01:05, listed now 709, rss 521 MB
  funding bybit MITO/USDT:USDT: data_unavailable: bybit does not have market symbol MITO/USDT:USDT
  funding bybit PTB/USDT:USDT: data_unavailable: bybit does not have market symbol PTB/USDT:USDT
  funding bybit VELODROME/USDT:USDT: data_unavailable: bybit does not have market symbol VELODROME/USDT:USDT
  guards: funding legs 0/916 differ (not compared 0), by venue binanceusdm 0/458 = 0.0 (not compared 0) -> ok, bybit 0/458 = 0.0 (not compared 0) -> ok -> ok; prices 0/5618 differ (not compared 0) -> ok

| book | cycles | decisions matched | accounting matched | APR vs cp_funding | payments vs cp_funding | (a.1) |
|---|---|---|---|---|---|---|
| A cc-paper | {'done': 107} | 314/314 | 2359/2359 | 298/298 (exempt {}) | 291/291 (exempt {}) | pass |
| B pp-paper | {'done': 107} | 553/553 | 7389/7389 | 2289/2289 (exempt {}) | 1056/1056 (exempt {}) | pass |

A by action: {'open': [7, 7], 'hold': [303, 303], 'close': [4, 4]}; unverifiable: []
  grid holes []; aside 16 (list in --out); cycles after the stop date without the flag: 0
  git SHAs with a mismatch: 0 of 101
  mismatches by kind {}

B by action: {'open': [23, 23], 'hold': [512, 512], 'close': [18, 18]}; unverifiable: []
  grid holes []; aside 2 (list in --out); cycles after the stop date without the flag: 0
    aside 2026-11-20 08:00 IRYS/USDT:USDT funding_payment_missing detail=payment_gap gap_cycles=1
    aside 2026-11-20 16:00 IRYS/USDT:USDT funding_payment_missing detail=payment_gap gap_cycles=2
  git SHAs with a mismatch: 0 of 101
  mismatches by kind {}

sha256 carry_replay.py 2089a30ce2577fe6, verify_carry_paper_replay.py 0278f398a4f60861, carry.py 1176c8b7db3787a8, carry_rules.py 4b04ac8c968cee32, market_data.py 1ecbe14557ab7d69; (a.2) stopped: None
caveat: (a.2) 'funding' variant: a symbol without a recorded candidate gets its APR from cp_funding as stored NOW (INSERT OR REPLACE, no write time), not as the engine saw it at t.
| book | decisions matched (rate) | open/close matched (rate) | divergences by class | skipped cycles | (a.2) | (a) |
|---|---|---|---|---|---|---|
| A cc-paper | 314/314 (1.0) | 11/11 (1.0) | {} | 0 | incomplete | ◯ |
| B pp-paper | 553/553 (1.0) | 41/41 (1.0) | {} | 0 | pass | ✓ |

A: by action [n, matched] {'open': [7, 7], 'close': [4, 4], 'hold': [303, 303]}; incomplete: ['funding + bars for 6/92 of the symbols that can matter']
  symbols 494, with funding 494, with bars 8, 1604 s
  coverage must 5/5, need 6/92, universe funding 491/491; delisted now ['MITO/USDT:USDT', 'PTB/USDT:USDT', 'VELODROME/USDT:USDT']
  cp_funding vs download: 491 legs in the window, compared 491, not compared 0, not listed 3; missing in engine 0, extra 0, rate differs 38; worst [['binanceusdm', '牛来/USDT:USDT', 0, 0, 38]]
  GUARD funding: legs with a difference 1/491 = 0.002036659877800407 (max 0.02) pooled -> ok; by venue binanceusdm 1/491 = 0.002036659877800407 (not compared 0) -> ok -> ok
  GUARD prices: recorded 1042, compared 1042, differ 0 (stale 0), not compared 0 (no bars 0, no bar at t 0), not listed 136: share 0.0 (max 0.01) -> ok; worst []
  slot competition {'rows': 0, 'several_roots': 0}; data_hold where the backtest would have STOPPED the set-aside position: 0 []

B: by action [n, matched] {'open': [23, 23], 'close': [18, 18], 'hold': [512, 512]}; incomplete: None
  symbols 461, with funding 458, with bars 324, 840 s
  coverage must 303/303, need 324/324, universe funding 458/458; delisted now ['MITO/USDT:USDT', 'PTB/USDT:USDT', 'VELODROME/USDT:USDT']
  cp_funding vs download: 916 legs in the window, compared 916, not compared 0, not listed 6; missing in engine 0, extra 0, rate differs 0; worst []
  GUARD funding: legs with a difference 0/916 = 0.0 (max 0.02) pooled -> ok; by venue binanceusdm 0/458 = 0.0 (not compared 0) -> ok, bybit 0/458 = 0.0 (not compared 0) -> ok -> ok
  GUARD prices: recorded 5618, compared 5618, differ 0 (stale 0), not compared 0 (no bars 0, no bar at t 0), not listed 16: share 0.0 (max 0.01) -> ok; worst []
  slot competition {'rows': 0, 'several_roots': 0}; data_hold where the backtest would have STOPPED the set-aside position: 0 []

full result: /home/tradedesk/.cache/tradedesk-agents/carry_replay/a2-1795564800000.json (rss 701 MB)
```

Перевірка знахідок (read-only, `~/.cache/tradedesk-agents/tmp/interim_probe.py`):

```
non-ascii: ['币安人生/USDT:USDT', '牛来/USDT:USDT']
colliding cache keys: {'_USDT_USDT': 2}
cache files with the empty base: ['binanceusdm__USDT_USDT_funding_1791734400000_1795568700000.json']
  币安人生/USDT:USDT: cp_funding 184 rows, file 267 rows, common ts 184, equal rate 184
  牛来/USDT:USDT: cp_funding 184 rows, file 267 rows, common ts 184, equal rate 143
lacking: 86
done cycles: 107
excluded as no_spot in every done cycle: 86/86; reason counts over symbol-cycles {'no_spot': 9202}
never excluded: []
lacking symbols ever a recorded candidate: []
```
