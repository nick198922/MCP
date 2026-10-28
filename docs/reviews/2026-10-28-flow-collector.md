# Рев'ю (режим C): колектор `flow_snapshot`, схема 32, `l2_book(n_sig_figs)`, увімкнення в `deploy/features.env`

Дата: 2026-10-28. Предмет: незакомічені зміни робочого дерева (картка [market-flow-reader](../ideas/IDEA-20261028-market-flow-reader.md), крок 2): `collector/flow_snapshot.py`, `analytics/flow_snapshot.py`, `store/schema.py` (32), `store/db.py`, `config.py`, `collector/scheduler.py`, `providers/hyperliquid.py`, `deploy/features.env`, `deploy/env.example`, `tests/test_flow_snapshot.py`, фікстура `flow_snapshot_live_2026-10-28.json`, документи.

**Вердикт: request changes** (один блокер, №1; решта — бажано в цьому ж PR).

## Що перевірено самостійно
- `make lint`: чисто (ruff check + format).
- `pytest tests/test_flow_snapshot.py tests/test_store.py tests/test_features.py tests/test_liquidations.py`: 37 passed (17 нових у `test_flow_snapshot.py`).
- Прогнав `features.load()` на чистому оточенні + `build_default_scheduler`: застосовано `FLOW_SNAPSHOT_ENABLED=1`, `LIQUIDATIONS_ENABLED=1`, `LIQUIDATIONS_VENUES=binance`, `WINDOW_SECONDS=600`; завдання — лише `flow_snapshot` і `liquidations:binance`. Тобто **сам** `features.env` справді обмежує ліквідації Binance.
- Інваріанти: мережа лише через `Exchanges` (`order_book`, `open_interest`, `funding_rate`, `funding_intervals`, `public_get`) і `Http`-провайдери (`binance_futures`, `bybit`, `hyperliquid`); прямих `httpx`/`websockets` у нових модулях немає. `analytics/flow_snapshot.py` чиста, без I/O і годинника. Кожна частина під `Builder.guard` → NULL + назва в `missing`; виняток-баг у одному рядку ловиться `gather(return_exceptions=True)`, `CancelledError` прокидається. Ключ кешу `Http` містить JSON-тіло, тож `l2Book` з `nSigFigs` 4 і 3 не злипаються в кеші.
- Міграція 32: один `CREATE TABLE IF NOT EXISTS … WITHOUT ROWID`, без змін наявних таблиць; тест міграції з v31 є. Таблицю не додано в `prune` (свідомо, без чищення) — коректно.
- Одиниці: funding — десятковий дріб за інтервал, річний = rate × 8760 / год (як `annualize_funding`); OI у монетах, OKX — `oiCcy`; книги OKX-свопу множаться на `ctVal` з перевіркою `ctValCcy == base`; ліміти книг (Binance spot 5000, USD-M 1000, Bybit spot 200 / linear 500) — максимуми REST.
- Тести: happy path на живих відповідях 28.10, часткові збої (NULL + `missing`), усі джерела впали (0 рядків, не кидає, відновлення наступним тіком), биті відповіді OKX, без сховища, ідемпотентність, вимкнено за замовчуванням, `nSigFigs` іде в запит. Тести впали б, якби зламати контрактне множення, смуги або логіку «лише відсутні ключі».
- Секретів у фікстурі й коді немає; коду ордерів немає; `features.env` проходить `test_the_shipped_file_is_valid`.

Не перевірено: `/etc/tradedesk-mcp.env` (читання заборонено), живі відповіді бірж (покладаюся на фікстуру), розмір таблиці `liquidations` на диску.

## Зауваження

1. **Блокер. «Лише Binance» тримається тільки на `features.env`, а він програє `/etc`.** `features.load` ніколи не перезаписує ключ, уже заданий в оточенні, а `scripts/server/16-collector-liquidations.sh` пише в `/etc/tradedesk-mcp.env` саме `TRADEDESK_COLLECT_LIQUIDATIONS_VENUES=binance,bybit,okx`. Крім того, дефолт у `config.py` — `"binance,bybit,okx"`: будь-хто, хто ввімкне `LIQUIDATIONS_ENABLED` без `VENUES` (або запустить скрипт 16 за старою інструкцією в HANDOFF), отримає парсери з відомими помилками: OKX `notional_usd = контракти × ціна` (для BTC з `ctVal` 0.01 — завищення в 100 разів), Bybit з інвертованою стороною (у v5 `S=Buy` — ліквідовано лонг). Ці числа йдуть у `liquidations_summary` і далі в контекст планів. Виправлення (дешеве):
   - дефолт `collect_liquidations_venues = "binance"` у `config.py` (і `from_env`), коментар у `deploy/env.example`;
   - у `scripts/server/16-collector-liquidations.sh` писати `VENUES=binance` (або позначити скрипт застарілим із посиланням на `features.env`);
   - тест: `Settings().collect_liquidations_venues == "binance"` і `build_default_scheduler` з `LIQUIDATIONS_ENABLED=1` без `VENUES` дає лише `liquidations:binance`;
   - після деплою перевірити в журналі рядок `features.env: …` і список завдань колектора (лише `liquidations:binance`); якщо `/etc` уже має `VENUES`, попросити власника прибрати ключ. Записати результат у HANDOFF.

2. **Середня. Частковий рядок години більше не дозбирується.** `flow_snapshot_keys` вважає годину виконаною для ключа, щойно є будь-який рядок, а `INSERT OR IGNORE` зберігає перший. Тимчасовий збій книги на першому тіку (наприклад, :00) робить глибину й `oi_usd` цієї години NULL назавжди, хоча до кінця години лишається ще 5 тіків; тест `test_partial_failures_leave_null_columns_named_in_missing` це навіть закріплює (Bybit perp ETH лишається з `mid=None`). Для історії, яку не можна відновити заднім числом, це зайва втрата. Виправлення: «готові» ключі — лише рядки з `missing IS NULL` (або без `book`/`oi`/`funding` у `missing`, щоб постійний `funding_interval` не крутився щотіку); вставка `INSERT … ON CONFLICT(ts, base, venue, market) DO UPDATE SET … WHERE flow_snapshot.missing IS NOT NULL AND <новий рядок має менше пропусків>`; `fetched_ts` — від тіку, що записав остаточний рядок. Тест: збій книги на першому тіку → на другому рядок замінено, `missing` NULL.

3. **Низька. Hyperliquid: пропуск funding не позначається, якщо монета є.** `missing=[…] + ([] if row else ["funding"])`: коли `metaAndAssetCtxs` має рядок BTC, але `funding_1h` = None, `funding_rate` буде NULL без `funding` у `missing`. Виправлення: додавати `funding`, якщо `row` є, а `funding_1h` немає (одним рядком).

4. **Низька. Покриття смуг Hyperliquid різне для BTC і ETH.** За поточних цін BTC має лише 0.1 % і 1 %, ETH — лише 0.5 % (тест це фіксує). Це задокументовано в HANDOFF лише частково («HL (BTC)»). Варто одним реченням у модулі/картці перелічити, які смуги HL реально заповнені, щоб крок 3 не порівнював HL з іншими біржами на неіснуючих смугах. Можна також додати запит із `nSigFigs=5` для ETH 0.1 %, але це вже розширення.

5. **Низька. Зберігання `liquidations`.** Таблиця не чиститься (її немає в `_retention_sweep`), оцінка ~25 тис. рядків/добу в спокійний день і ~1 ГБ/рік; у каскадні дні — в рази більше. Задокументовано в BACKLOG як відкрите питання, вільного місця достатньо, тож не блокер; але бекапи ростуть разом із нею. Пропоную окремим кроком `TRADEDESK_RETENTION_LIQUIDATIONS_DAYS` (наприклад, 730) за зразком інших таблиць.

6. **Інфо. HANDOFF:** у рядку «Сховище» лишилося «схема 26», а тепер 32. Поправити заодно.

## Висновок
Код колектора акуратний: інваріанти дотримано, тести змістовні, міграція безпечна, `features.env` сам по собі вмикає лише Binance. Блокує одне: обмеження «лише Binance» не закріплене в коді й перебивається `/etc` та скриптом 16, а відомі баги парсерів OKX/Bybit дають хибні числа ліквідацій (×100 і інвертовану сторону). Після №1 (і бажано №2) — approve.

## Повторний огляд (2026-10-28, раунд виправлень)

**Вердикт: approve.**

### Що перевірено самостійно
- `make lint`: чисто (ruff check + format --check, 1878 файлів).
- `pytest tests/test_flow_snapshot.py tests/test_liquidations.py tests/test_store.py tests/test_features.py`: 39 passed.
- Прочитав `git diff` для `config.py`, `store/db.py`, `collector/flow_snapshot.py`, `scripts/server/16-collector-liquidations.sh`, `deploy/env.example`, `deploy/features.env`, `tests/test_liquidations.py`, `tests/test_flow_snapshot.py`, HANDOFF, BACKLOG, картки й `docs/changes/`. Grep по репозиторію: старий дефолт `binance,bybit,okx` лишився тільки в історичних `CHANGELOG.md` і `docs/history/`.

### Статус зауважень першого огляду
1. **Блокер (venues) — виправлено.** Дефолт `collect_liquidations_venues = "binance"` і в полі `Settings`, і в `from_env`. Скрипт 16 тепер пише `VENUES=binance`, у `deploy/env.example` є коментар із причиною. Тест `test_enabling_liquidations_without_venues_starts_binance_only` вмикає ліквідації без `VENUES` і перевіряє, що `build_default_scheduler` створює лише `liquidations:binance`. Тест упав би, якби дефолт повернули. У HANDOFF записано перевірку після деплою (у журналі MCP лише `liquidations:binance`, інакше власник прибирає ключ з `/etc`). Сам я цього не перевіряв: `/etc` агентам не читається. Нагадування координатору: `scripts/server/services-check.sh` уже показує `COLLECT_LIQUIDATIONS_VENUES` з `/etc`, тож власник може перевірити це однією командою.
2. **Середня (часткові рядки) — виправлено.** Логіку перевірив по коду (`Store._flow_gaps_sql`, `insert_flow_snapshot`, `flow_snapshot_keys`):
   - `missing` — відсортований унікальний список через кому або NULL (`analytics/flow_snapshot.flow_row`). Число пропусків = кількість ком + 1, мінус 1, якщо є `funding_interval`. NULL дає 0. Порожнього рядка `''` колектор не пише. Інша назва не містить підрядка `funding_interval`.
   - Upsert `ON CONFLICT(ts, base, venue, market) DO UPDATE … WHERE gaps(excluded) < gaps(stored)`. Повний рядок (0 пропусків) не замінюється нічим, бо менше за 0 не буває. Рівна кількість пропусків теж не замінює. Тест на рядку 407 підтверджує, що повний рядок `funding_interval` лишається остаточним. Ключ включає `ts`, тож попередні години не зачіпаються.
   - `flow_snapshot_keys` повертає лише рядки з 0 пропусків. Частковий ключ перезапитується, а «без даних» (`has_data` = false) не пишеться, тож старий частковий рядок не затирається порожнім.
   - `total_changes` не рахує UPDATE, відхилений через `WHERE`. Тест (`== 0` на рядках 397, 398 і 407) це закріплює.
   - Тест колектора: збій на першому тіку, відновлення на другому. Вставлено 2 відсутні рядки й замінено 6 часткових, `fetched_ts` новий, повний рядок HL зберіг старий `fetched_ts`. На третьому тіку запитів 0.
3. **Низька (HL funding) — виправлено.** Рядок 203 позначає `funding`, якщо монета є, а `funding_1h` немає. Додано тест `test_hl_coin_without_funding_names_funding_in_missing`.
4. **Низька (смуги HL) — виправлено** у docstring модуля, HANDOFF і картці (BTC: ±0.1 % і ±1 %, ETH: ±0.5 %).
5. **Низька (зберігання `liquidations`) — відкладено свідомо:** питання записано в BACKLOG у пункті про Bybit/OKX. Для approve цього достатньо.
6. **Інфо (HANDOFF «схема 26») — виправлено** на 32.

### Нові зауваження (не блокують)
7. **Низька. Заміна цілим рядком може викинути частину, яка в старому рядку була.** Рядок замінюється, коли пропусків менше, а не коли новий набір пропусків є підмножиною старого. Приклад із тесту (рядки 399–402): старий `missing="book,long_share"` з `oi_coins=31188`, новий `missing="oi"`. Після заміни `oi_coins` = NULL, хоча OI цієї години вже був отриманий. Повний рядок так не втрачається, і ключ дозбирається на наступних тіках, тож це рідкісний випадок без систематичного зміщення. Якщо захочеться виправити: замінювати лише тоді, коли множина пропусків нового рядка є підмножиною старої (у Python перед вставкою), або зливати колонки через `COALESCE` і перераховувати `missing`. Друге змішує час вимірювання в одному рядку, тому краще перше.
8. **Низька. Опис MCP-інструменту ліквідацій** (`tools/liquidations.py`, рядок 25) досі каже «Venues: Binance USD-M, Bybit, OKX». Модель, що читає опис, може чекати даних Bybit/OKX, яких тепер немає. Виправлення: «Binance USD-M by default; Bybit/OKX only if enabled (their parsers are known to be wrong, see BACKLOG)». Зробити окремим дрібним кроком або разом із виправленням парсерів.
9. **Інфо. Вартість постійного часткового ключа.** Якщо одна частина (наприклад, rubik OKX або `/futures/data` Binance) падає годинами, ключ перезапитується повністю щотіку: до 6 разів на годину замість 1, тобто ~40 → ~240 запитів на годину в найгіршому разі. Це в межах rate-limit провайдерів і видно в логах (`partial rows`), тож дій не потрібно. Для кроку 3: рядок, дозібраний пізніше, має знімок не з початку години, і справжній час вимірювання показує `fetched_ts`.

### Висновок
Блокер №1 закрито в коді, скрипті, прикладі оточення й тестом. Дозбирання часткових рядків (№2) коректне: повний рядок ніколи не перезаписується, порожня відповідь не затирає частковий рядок, повтор ідемпотентний. №7–9 не блокують. **Approve.** Після деплою потрібна одна ручна перевірка: у списку завдань колектора лише `liquidations:binance`.
