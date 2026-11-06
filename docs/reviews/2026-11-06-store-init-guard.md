# Рев'ю: `store_ready` / `store_failed` для 28 інструментів сховища (пункт 165, BACKLOG 6.42)

Режим C (код). Предмет: незакомічений diff робочого дерева (`git diff -w`), нотатки автора `docs/changes/2026-11-06-store-init-guard.md`.

**Вердикт: request changes.** Один блокер (знахідка 1), решта не блокує.

## Що перевірив сам

- `git diff -w` усіх 9 модулів `tools/`, `_util.py` і тесту. Логіка тіл не змінилась: лише відступ під `try`, перенесення рядків від `ruff format`, заміна `_store_guard` + `init()` на `store_ready`. Тексти `config` (`the journal`, `events`, `rules`, `positions`) ті самі, що були.
- `Store.init()` ідемпотентний (`db.py:128`, `if self._conn is not None: return`), тож перенесення `init()` перед перевіркою входу в `plan_add`, `carry_paper_open`, `assistant_paper_open` на робочому сховищі нічого не змінює.
- `make lint`: чисто.
- `pytest` для `test_tools_no_store`, `test_journal`, `test_journal_rules`, `test_events`, `test_event_study`, `test_rules`, `test_positions`, `test_trade_plan`, `test_assistant_paper`, `test_carry_paper`, `test_carry_paper_live`, `test_paper_carry_report`, `test_forecast_log`, `test_common_helpers`, `test_common_helpers_callers`, `test_server`, `test_architecture_imports`: 328 passed.
- Тест чесний у головному: якщо прибрати `except STORE_ERRORS` з будь-якого інструмента, який доходить до запиту, `OperationalError` вийде як помилка MCP, `raised` стане непорожнім і `test_store_query_error_goes_to_missing` впаде.
- Дубль `rules_add` перевірив окремим скриптом на справжньому `Store` (файл, WAL): див. знахідку 1.

## Знахідки

1. **Блокер. `rules_add` з наявною назвою: помилка користувача видається за збій сховища, а з'єднання лишається з відкритою транзакцією запису.**
   `Store.rule_insert` (`db.py:1442`) робить `INSERT` без відкату. Python `sqlite3` перед `INSERT` неявно відкриває транзакцію; після `IntegrityError` вона лишається відкритою. Перевірено на файлі у WAL: після другого `rule_insert(name="dup")` `conn.in_transaction == True`, а запис з іншого з'єднання (так пише колектор) отримує `OperationalError: database is locked`, доки MCP-процес не закомітить щось інше. До змін було те саме, але виняток хоча б виходив назовні; тепер інструмент «обробляє» його, повертає `missing: store backend_error "IntegrityError: UNIQUE constraint failed: rules.name"`, і асистент скаже власнику, що зламане сховище, а не що назва зайнята. Саме цей випадок ховає реальну проблему.
   Виправлення (мінімальне, в межах пункту): у `rules_add` перед вставкою перевірити назву (`rules_list()` або новий `rule_get_by_name`) і повертати `missing: name`, `invalid_input`, `"rule name already exists (id=N)"`, `ok=false`; у `Store.rule_insert` на `sqlite3.IntegrityError` робити `await self.conn.rollback()` і перекидати далі (на випадок гонки двох викликів). Тест: двічі `rules_add` з однією назвою → другий дає `invalid_input` на `name`, `rt.store.conn.in_transaction is False`.

2. **Середня, не блокує (у BACKLOG). Після будь-якої помилки запису `store_failed` не відкочує транзакцію.** Те саме, що в п. 1, для решти записів (`journal_add`, `plan_link_journal` з `foreign_keys=ON`, `positions_close` тощо): якщо помилка виникла після неявного `BEGIN`, з'єднання тримає блокування запису. Загальний відкат у `store_failed` небезпечний (спільне з'єднання: можна відкотити чужий незакомічений запис, див. коментар у `db.py:998-1002`), тому правильне місце — методи `Store`, як уже зроблено в `carry_paper_close_claim` (`db.py:1031`). Додати пункт у BACKLOG поруч із 6.43: «методи запису `Store` без `rollback` на помилці».

3. **Низька. Тест перевіряє лише 6 з 23 заявлених `backend_error`.** `assert {"journal_list", …, "events_list"} <= failed` пройде, навіть якщо 17 інших інструментів почнуть повертати, наприклад, `ok=true` з порожніми даними замість `missing`. Виправлення: явна множина очікуваних винятків (п'ять, названих у нотатках) і `assert failed == QUERY_GUARDED - EXPECTED_NO_QUERY`, плюс `ok is False` для всіх із `failed`, крім `paper_carry_status`.

4. **Низька. `except STORE_ERRORS` охоплює все тіло, включно з мережевими кроками** (`carry_paper_open`, `assistant_paper_open`, `forecast_log_status` → `settle_due`, `plan_review` → `review_open`, `event_study`, `rules_test`). `OSError` не зі сховища (наприклад, `ssl.SSLError`, якщо провайдер колись його пропустить) буде підписано як `missing: store`. До змін він виходив назовні, тож гірше не стало, але підпис хибний. Якщо лишати так, у `store_failed` варто писати `detail` з типом (уже є) і не робити висновків про сховище з одного `OSError`; або звузити `try` до викликів `rt.store.*`. Не блокує.

5. **Низька, інформативно. Часткові записи перед помилкою не видно у відповіді.** `plan_review`, `forecast_log_status`, `assistant_paper_status` спершу закривають/розраховують рядки (комітять), потім читають; збій читання дає `data=null`, і кількість уже закритих у відповідь не потрапляє. Раніше був виняток, тож це не регресія; наступний виклик покаже стан. Можна не чіпати.

6. **Без зауважень.** `store_ready` ловить `Exception` лише навколо `init()` (так само, як інструменти історії); на старті сервера lifespan відкриває сховище сам, тож зламана міграція не сховається. Зміна порядку перевірок у трьох `*_open`/`plan_add` торкається лише режиму недоступного сховища. Нових інструментів немає, тому README/PARITY не потрібні; запис у `docs/changes/` є, BACKLOG 6.42/6.43 оновлено. Коду ордерів, ключів, мережі поза провайдерами немає.

## Що потрібно для approve

- Виправити п. 1 (перевірка назви + відкат у `rule_insert` + тест).
- Бажано: п. 3 (точна множина в тесті) і рядок у BACKLOG для п. 2.

## Раунд 2

Вердикт: **approve**.

Перевірено сам: git diff чотирьох файлів, `make lint` (чисто), `pytest tests/test_rules.py tests/test_tools_no_store.py tests/test_server.py` (96 passed).

1. Блокер п. 1 закрито. `rules_add` перевіряє назву через `rules_list` до вставки: дубль повертає `ok=false`, `missing: name / invalid_input` з id наявного правила. `Store.rule_insert` на `sqlite3.IntegrityError` робить `rollback()` і перекидає виняток, тож гонка з колектором не лишає відкритої транзакції та write-lock. Інші помилки сховища ловить `STORE_ERRORS` -> `store_failed`.
2. Тести б'ють по обох шляхах. `test_rules_add_duplicate_name_is_invalid_input` перевіряє `in_transaction is False`, поле й причину, а також що в списку одне правило. `test_rule_insert_duplicate_rolls_back` дублює вставку напряму в сховище, після чого запис з другого з'єднання (timeout 0.1) проходить; без відкату він упав би на locked.
3. Попутно `test_store_query_error_goes_to_missing` покриває помилку sqlite під час запиту; множина `QUERY_GUARDED` з винятками `NO_QUERY_WITH_ARGS` задана явно (це закриває п. 3).

Залишкове (не блокер): перевірка назви й вставка не атомарні, але гонка тепер безпечна завдяки відкату.
