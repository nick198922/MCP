# Рев'ю: funding_carry_scan, позначка incomplete_history (пункт 251)

Режим C. Вердикт: **approve**. Блокерів немає.

Перевірено сам: `git diff`, `make lint` (чисто), `tests/test_funding_carry_incomplete.py` + `tests/test_server.py` (81 passed).

## Перевірка за пунктами
1. Зміна коду: `tools/perps.py` змінює лише `funding_carry_scan`. Ряд отримує `incomplete_history`. `summary.above_entry_with_spot` виключає такі символи. З'являється `summary.incomplete_history`. Попередження називає біржу й символ. `analytics/` не чіпали, мережа йде лише через `rt.exchanges`.
2. Інструмент не кидає виняток: збій історії йде через `b.guard` у `missing`, `got` порожній, `incomplete=False`. Тест `test_history_failure_goes_to_missing_not_incomplete` це перевіряє.
3. Опис інструмента англійською, з поясненням нового поля та наслідків для `above_entry_with_spot`.
4. Документація: `README.md` і `docs/PARITY.md` оновлено, є запис у `docs/changes/`. `BACKLOG.md` і `HANDOFF.md` змінено на один рядок кожен. Окремого запису в `CHANGELOG.md` немає, але за CLAUDE.md агенти ведуть `docs/changes/`, тож це нормально.
5. Якість тестів: без виправлення тести впали б, бо ключа `incomplete_history` у рядках немає (KeyError), а символ лишався б у `above_entry_with_spot`. Другий тест іде через справжній `Exchanges` із відкинутим рядком (`timestamp: null`). Третій перевіряє, що позначено лише ETH. Четвертий перевіряє, що збій провайдера не дає позначки.
6. Обсяг: інших змін немає (5 файлів у diff плюс 2 нових).

## Зауваження (не блокують)
1. Низька серйозність. Умова змінилась з `not got["complete"]` на `got.get("complete") is False`. Якщо провайдер колись віддасть `complete` без ключа або з `None`, це трактуватиметься як повна історія (раніше навпаки). Зараз провайдер завжди повертає булеве значення, тому на практиці різниці немає. Для консервативності можна було б лишити `not got.get("complete", True)` або `not got.get("complete")`.
2. Низька серйозність. Нового поля `incomplete_history` у рядку немає в `docs/changes/`-описі форми відповіді для споживачів (скрипти/плани, що читають `rows`). Додавання ключа сумісне назад, тож це лише примітка.
