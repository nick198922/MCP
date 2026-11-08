# Рев'ю (режим C): ccxt-фандинг, `dropped_rows` і `complete=false` (пункт 206)

Предмет: незакомічений diff (`src/tradedesk/providers/exchanges.py`, `tools/perps.py`, `tools/carry_paper.py`, `tests/test_pagination.py`, `docs/BACKLOG.md`, рядок 206 постійної картки, `docs/changes/2026-11-07-ccxt-funding-dropped-rows.md`).

**Вердикт: request changes.** Сам провайдер зроблено правильно. Але новий `complete=false` потрапляє в рушій паперового carry і там у сценарії, заради якого цей пункт і робили, ламає гарантію R2 «утримана позиція не висить вічно».

## Що перевірив сам

- `make lint`: чисто.
- `pytest tests/test_pagination.py tests/test_carry_paper.py tests/test_carry_paper_live.py tests/test_carry_paper_engine.py tests/test_cycle_regime.py tests/test_cycle_regime_journal.py tests/test_tools_never_raise.py tests/test_tools_envelope_shape.py`: 135 passed.
- `trust_short_page` за сирим розміром сторінки (`exchanges.py:339`). Нескінченного циклу немає: кожна ітерація збільшує `pages`, і цикл обмежений `max_pages`. Гілка `nxt <= since` так само зупиняє цикл, а порожній `batch` зсуває `since` на `page` годин, як і раніше. Якщо всі рядки валідні, то `len(data) == len(batch)`, тож для чистих даних поведінка й числа ті самі. Bybit-гілка (`:295`) і раніше дивилася на `len(f.data or [])`. Логіку вікна (`since_ms <= t <= end`, перевірка `BYBIT_NEWEST_SLACK_MS`, `end = min(batch) - 1`) не змінено, рядки поза вікном у `dropped` не рахуються.
- Тести впали б на старому коді. `test_ccxt_funding_range_malformed_timestamp_marks_incomplete` дає `KeyError` на `dropped_rows` і `complete=True`. `test_..._does_not_end_a_trusted_walk_early` на старому коді зупинився б після першої сторінки (99 рядків замість 247).
- Інваріанти: мережа так і йде через `funding_rate_history` → `Exchanges._call`. Нових винятків з інструментів немає. Попередження англійською. Запис у `docs/changes/` є. Ордерного коду й ключів немає. Поза пунктом нічого не змінено.

## Знахідки

1. **[висока, блокер] Утримана позиція в `carry_paper_engine` висить вічно, якщо зіпсований рядок і є тим платежем, якого бракує.**
   Ланцюжок такий. Майданчик віддає платіж, у якого ccxt не розібрав час (`timestamp=None`).
   - Рушій не бачить платежу, `_funding_issue` повертає «бракує», `gap_cycles` стає 1 (`carry_paper_engine.py:420-429`).
   - На наступному циклі фід перечитує вікно від `last` позиції (`carry_paper_feed.py:131-136`, `:169-178`). Це вікно містить зіпсований рядок, тому тепер `complete=false` і фід ставить прапор `funding_history_incomplete`.
   - Рушій для символу з цим прапором «тримає позицію, не рахуючи цикл до підтвердження» (`carry_paper_engine.py:410-415`). `gap_cycles` лишається 1, наступний цикл знову перечитує те саме вікно, і так без кінця. `GAP_CONFIRM_CYCLES = 3` ніколи не настає, `funding_gap_confirmed` не пишеться, позиція не розраховується.
   - До зміни рядок мовчки відкидався, `complete=true`, і після 3 циклів позиція розраховувалась із `funding_gap_confirmed` і подією про недоплату.

   Прапор `funding_history_incomplete` означає «наш запит обрізано лімітом сторінок або 429/418, майданчик не винен» (коментар `:411-412`, текст `analytics/checkpoint.py:497`). Зіпсований рядок — дефект даних майданчика: повторний запит його не виправить. Тут він потрапляє не в той клас. Запис у `docs/changes` прямо каже «Фід … ставить `funding_history_incomplete`, як і на обрізаному гортанні», тобто це не врахували.

   **Виправлення.** Відокремити причину від `complete` і не змішувати обрізання з відкинутими рядками:
   - у `data` провайдера додати `"dropped_rows": n` (або `"truncated": bool`) поряд з `complete`;
   - у `carry_paper_feed._history`, якщо `complete=false` лише через відкинуті рядки (`truncated` false), ставити окремий прапор, наприклад `funding_rows_dropped`. Рушій не має заносити його в `incomplete` (`:401`), щоб R2 рахував цикли і за `GAP_CONFIRM_CYCLES` підтверджував розрив. Покриття (`covered_from/to`) у такому разі все одно не записувати;
   - тест у `tests/test_carry_paper_engine.py` або `test_carry_paper_live.py`: фейк, що стабільно віддає платіж із `timestamp=None` → за 3 цикли позиція розраховується з `funding_gap_confirmed`, а не тримається вічно.

   Інший варіант: лишити `funding_history_incomplete`, але рахувати такий цикл до підтвердження, коли причина — відкинуті рядки. Головне, щоб тест на завершення R2 був.

2. **[низька] Застарілі пояснення прапора.** Коментар `carry_paper_engine.py:411` («page limit, 429/418») і текст `analytics/checkpoint.py:497` («наш запит обрізано лімітом сторінок або 429 / 418») після зміни неточні, якщо п. 1 розв'яжуть не окремим прапором. Виправлення: оновити обидва разом із п. 1.

3. **[низька] `dropped_rows` рахує спостереження, а не унікальні рядки.** Час зіпсованого рядка невідомий, тому прибрати повтори неможливо. В обох випадках нижче той самий рядок порахують двічі:
   - у Bybit-гілці наступне вікно `[since_ms, min(batch) - 1]` знову поверне рядок, справжній час якого старіший за `min(batch)`;
   - у прямій гілці повну сторінку, де зіпсовано всі рядки, обходять зсувом на `page` годин, і вікна перекриваються.

   На `complete` це не впливає. Виправлення: у docstring і в `docs/changes` написати «кількість відкинутих рядків у відповідях (повтор у сусідніх сторінках рахується двічі)».

4. **[низька, було й раніше] Рядок не-`dict` кидає `AttributeError` з провайдера.** `x.get("timestamp")` у `exchanges.py:290/:328` впаде на `None` чи рядку в списку. `Builder.guard` ловить лише `ProviderError`, тож виняток вийде з інструмента (інваріант 1). У Hyperliquid-гілці є фільтр `isinstance(x, dict)` (`hyperliquid.py:246`). Розібраний ccxt такого практично не віддає, але заради паритету варто додати той самий фільтр і рахувати такі рядки в `dropped`. Не блокер.

5. **[інформація] Відкрите, як і зазначено в записі.** `_rows_of` у `tools/cycle.py` / `tools/flow.py` досі пише «paging stopped early», а `funding_spread_scan` (`perps.py:654`) не дивиться на `complete`. Записано в «Не зроблено», для цього кроку прийнятно.

## Для повторного рев'ю

Після п. 1 (і бажано п. 3) перезапустити `tests/test_pagination.py`, `tests/test_carry_paper*.py`, `tests/test_checkpoint_report.py` і новий тест на завершення R2.

## Повторне рев'ю (07.11, після раунду виправлень)

**Вердикт: approve.** Блокера більше немає, нового блокера не знайшов.

### Що перевірив сам

- `make lint`: чисто (3272 файли).
- `pytest` для `test_pagination`, `test_carry_paper`, `test_carry_paper_live`, `test_carry_paper_engine`, `test_checkpoint_report`, `test_cycle_regime`, `test_cycle_regime_journal`, `test_tools_never_raise`, `test_tools_envelope_shape`, `test_tools_wrong_type_fields`: 373 passed. Це збігається з числом у записі змін.
- **П. 1 (блокер) — виправлено.** Провайдер повертає `_range_data(rows, walked, dropped)` = `{"rows", "complete": walked and not dropped, "truncated": not walked, "dropped_rows"}` (`exchanges.py:42-44`), в обох гілках. `carry_paper_feed._flag_incomplete` (`:141-151`) ставить `funding_history_incomplete` лише при `truncated`; за замовчуванням `truncated=True`, тобто обирає безпечний бік. Якщо гортання дійшло до кінця і є тільки відкинуті рядки, ставиться `funding_rows_dropped` з `dropped_rows`. Обидва шляхи фіду кличуть ту саму функцію: перечитування утримуваної позиції (`:190`) і звичайний (`:207`). Рушій збирає `incomplete` лише з `funding_history_incomplete` (`carry_paper_engine.py:401`). Інших споживачів `data.flags` у рушії немає, крім `complete: not flags` і лічильника (`:564-582`). Отже на таких циклах R2 рахує `gap_cycles` і на `GAP_CONFIRM_CYCLES` підтверджує розрив. Покриття при `complete=false` не записується (`:214-215`, на шляху перечитування `None` і так). Синтетичний `{"rows": [], "complete": True}` (`:196`) у `_flag_incomplete` одразу виходить, тому відсутній ключ `truncated` йому не шкодить.
- **Новий тест** `test_payment_with_a_null_timestamp_is_confirmed_as_a_gap_not_held_forever` впав би в обох можливих регресіях. Якщо фід знову ставитиме `funding_history_incomplete`, падає перевірка `[f["issue"] …] == ["funding_rows_dropped"]`. Якщо рушій почне відносити новий прапор до `incomplete`, `gap_cycles` застрягне на 1 і впаде `== n` при n=2, а заодно й подія `funding_gap_confirmed`. Тест покриває обидва шляхи фіду: на n=1 звичайний інкремент, на n=2 і n=3 перечитування від `last`.
- **П. 2 — виправлено.** Коментар рушія (`:411-414`) і текст (д) / коментар у `analytics/checkpoint.py` (`:452-454`, `:498-499`) описують обидва прапори. Розбивка `issues` за issue / detail покаже `funding_rows_dropped` окремим рядком.
- **П. 3 — виправлено.** Docstring (`exchanges.py:264-273`) і запис змін прямо кажуть, що лічаться появи рядка у відповідях, а не унікальні рядки.
- **П. 4 — виправлено.** Обидві гілки фільтрують `isinstance(x, dict)` і рахують відкинуте в `dropped`. `test_ccxt_funding_range_non_dict_row_is_dropped_not_raised` (binanceusdm і bybit × `None` / `"row"` / `7` / `["x"]`): рядки ті самі, `dropped_rows=1`, винятку немає.
- **Інші виклики `funding_rate_history_range`.** Ніхто не розпаковує результат кортежем і не порівнює словник цілком. `tools/perps.py:349/507`, `tools/carry_paper.py:88/286` читають `got["complete"]` / `got["rows"]`, а попередження тепер називають і відкинуті рядки. `tools/cycle.py:48` і `tools/flow.py:67` (`_rows_of`) читають через `.get`. Нові ключі нічого там не ламають, лише текст «paging stopped early» неточний (п. 5, записано в «Не зроблено»). `funding_spread_scan` (`perps.py:654`) не дивиться на `complete`, як і раніше. Скрипти `scripts/verify_*` беруть `f.data["rows"]` / `["complete"]`; додаткові ключі сумісні. Єдине порівняння словника цілком, `test_pagination.py:351`, стосується Hyperliquid-гілки, якої зміна не торкнулась.

### Нові зауваження (не блокують)

6. **[інформація] `funding_rows_dropped` знижує частку «повних» циклів.** Прапор ставиться для будь-якого символу, який читає фід, не лише для утримуваних, і робить цикл `complete=false` (`carry_paper_engine.py:569`). Тож дефект даних майданчика тепер видно в критерії (д) CHECKPOINT, а раніше такий цикл мовчки рахувався повним. Це чесніше, а примітка (д) і розбивка за issue пояснюють причину. Змін не треба. Варто пам'ятати: якщо (д) не пройде, спершу подивитися частку `funding_rows_dropped`.
7. **[низька, було й раніше] Покриття згодом «заклеює» дірку.** На звичайному шляху інкремент іде від `last_ts + 1`. Тож коли пізніший валідний рядок збережено, зіпсований рядок більше не перечитується, і наступне повне читання записує `covered_from` (старе) … `until` поверх дірки. Для утримуваної позиції це ловить перечитування R2 (новий тест), для решти — ні. Це дизайн, що був до цього пункту, а не регресія. За бажання можна додати рядок у «Не зроблено» запису змін.
