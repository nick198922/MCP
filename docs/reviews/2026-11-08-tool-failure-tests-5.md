# Рев'ю: тести збою, частина 5 (пункт 287), events_* і journal_*

Режим C. Дифф: `src/tradedesk/tools/events.py`, `src/tradedesk/tools/journal.py`, `tests/test_tool_failure_extra_5.py`, `docs/changes/2026-11-08-tool-failure-tests-5.md` (плюс правки BACKLOG, HANDOFF, постійної картки, які я не перевіряв по суті).

**Вердикт: approve.** Блокерів немає.

## Що перевірив сам

- `make lint`: чисто.
- `uv run pytest -q tests/test_tool_failure_extra_5.py`: 66 passed (збігається з числом у `docs/changes/`).
- Суміжні тести: `test_journal`, `test_events`, `test_tool_failure_extra_3`, `test_tools_corrupt_store`, `test_tools_bad_input`, `test_tools_never_raise`, `test_tools_no_store`, `test_journal_*_tg`, `test_journal_rules`, `test_store`, `test_macro_calendar`: 202 passed. Повний набір не запускав (це робить координатор).
- Допоміжні функції `reject_nonfinite`, `reject_nonfinite_result`, `first_nonfinite`, `drop_corrupt_rows` вже є в `_util.py`, його дифф не чіпає.

## Інваріанти

- Інв. 1 (інструмент не кидає): усі нові гілки повертають `b.build(..., ok=False)` з `missing`. Закриті шляхи: `OverflowError` на `opened_ts`/`closed_ts` = 10**20, `float()` на рядку в `size`/`stop`, `AttributeError`/`TypeError` на seed не у формі `{"events": [...]}`, `OverflowError` на ts = 1e308. Тести це відтворюють.
- Інв. 4 (одиниці): секунди в журналі і в seed, `ts_ms` в `events_add` не змінено. Мілісекунди в `opened_ts`/`closed_ts` тепер відхиляються, бо в описі параметра сказано «unix seconds». Це відповідає контракту. Внутрішні виклики (`demo_trader`, `carry_paper_engine`) пишуть у сховище напряму і діляться на 1000, тож не зачеплені.
- Інв. 5: нових інструментів немає, `EXPECTED_TOOLS`/README/PARITY не потрібні; запис у `docs/changes/` є. `execution/` не чіпали.

## Регресії на коректних даних

Перевірив, що тести фіксують незмінність: `test_journal_close_valid_trade_unchanged`, `test_journal_stats_large_but_safe_values_unchanged`, `test_events_list_empty_and_valid_unchanged` (у тому числі не-JSON рядок у `meta` лишається текстом), `test_events_seed_load_bundled_seed_loads_without_skips` (справжній seed завантажується без пропусків, повторний виклик ідемпотентний), `test_events_add_extreme_but_valid_input_is_stored` (рік 9999, 1e308, "inf" як текст). Порожній `events: []` у seed тепер `ok=false` замість `ok=true` з нулем. Це свідома зміна, покрита тестом, на реальному seed не впливає.

## Зауваження (не блокують)

1. **Низька.** `journal_close` з `cancelled=true` теж відмовляє на зіпсованому відкритому рядку («malformed; it was not closed»). Власник не може скасувати такий рядок через інструмент. Тест є лише для не-cancel шляху. Варто або дозволити cancel (він не рахує PnL), або зафіксувати поточну поведінку тестом.
2. **Низька.** `events_list` відсікає рядки за `store_num(ts)`, але не за діапазоном datetime (`_event_ts`), яким користується `events_study`. Рядок із ts = 1e300 проходить `events_list` і відсікається лише в study. Не помилка, але два фільтри не однакові.
3. **Низька.** Для `meta` з нерозбірним для JSON значенням (`TypeError`) повідомлення скаже «NaN or inf». Через MCP-вхід (JSON) це недосяжно.
4. **Інформаційно.** Відкрите у `docs/changes/`: `journal_list` на blob з невалідним UTF-8 не перевірено. Нехай лишиться пунктом на наступний крок.
5. **Інформаційно.** `impact` у рядках seed не валідується (нетекстове значення стане `str(...)`). Безпечно, лише косметика.

## Якість тестів

Тести падали б без правок: кожен баг із опису має параметризований кейс з перевіркою `ok`, `missing` і стану сховища (рядок лишається `open`, нічого не вставлено). Хелпер `_session` додатково перевіряє, що в тексті відповіді немає NaN/Infinity (`first_nonfinite`), тож «тихий» Infinity теж зловили б. Тест `events_add` з нескінченною `meta` свідомо викликає `call_tool` напряму, бо клієнт серіалізує inf як null; пояснено в коментарі.
