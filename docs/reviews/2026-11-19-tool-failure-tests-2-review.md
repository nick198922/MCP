# Рев'ю: тести збою для 8 інструментів + event_study ok=false (пункт 396, BACKLOG 6.53)

Режим C. Вердикт: **approve**. Блокерів немає.

## Що перевірено особисто
- `make lint`: чисто.
- `uv run pytest -q` на `test_tool_failure_extra_26.py` та файлах, що згадують `event_study` або покривають усі інструменти (`test_event_study`, `test_tools_envelope_shape`, `test_tools_never_raise`, `test_tools_bad_input`, `test_tools_no_store`, `test_tool_failure_extra_2`, `test_tool_failure_extra_25`, `test_json_cells_guard`, `test_tool_desc_audit`): 136 passed. Повний набір не запускався, як і просили.
- `git diff` по `src/tradedesk/tools/events.py`: змінено лише `event_study` (гілка `miss` + `ok=evaluated` + опис). `execution/`, `core/`, `providers/` не торкнуто.

## Інваріанти
1. Виняток назовні: `ok=false` ставиться через `Builder`, а не через raise. `STORE_ERRORS` -> `store_failed` лишився. Тести перевіряють відповідь через `Client`, `is_error` не виставляється. Дотримано.
2. Мережа: `event_study` не має провайдера. Тести підтверджують `calls == []` (жодного HTTP/ccxt) для всіх store-інструментів. Дотримано.
5. Опис англійською, одиниці збережені, застереження про n_sample додано. Нового інструмента немає, тож `EXPECTED_TOOLS`/README/PARITY не потрібні. Запис у `docs/changes/` є, окремим файлом. Дотримано.

## Споживачі event_study
Grep по `src/` і `tests/`: у коді інструмент викликають лише промпти (`prompts/__init__.py`, `fomc_playbook.md` як текстову інструкцію LLM) і правила-пресети (лише згадка в тексті). Програмних споживачів, що читали б `ok=true` з порожніми статистиками, немає. `data` при `ok=false` зберігається (список подій), тож клієнт, який читає `per_event`, не ламається. Вимога `ok=true` при `n_events_evaluated=0` у наявних тестах не знайдена, усі пройшли.

## Якість тестів
Тести перевіряють вміст конверта, а не тривіальність:
- `_missing(env, field, reason, detail)` звіряє поле, причину й підрядок деталі (`TRADEDESK_DB_PATH`, `disk I/O error`, `no event`).
- Перевіряється, що `calls == []`, `data is None` для збою сховища, `n_events_requested/evaluated`, `per_event` зі значеннями `None`.
- Є контрольний тест `partly_evaluated_stays_ok`: межа між `ok=true` і `ok=false` зафіксована з обох боків, тому тест на `ok=false` не проходить випадково.
- `first_nonfinite` ловить NaN/Infinity у `data`.

## Зауваження (не блокують)
1. Низька. `test_store_tools_answer_with_every_provider_down` перевіряє лише `ok=true`, `missing == []`, `data is not None`. Вміст `data` не перевіряється, тож неправильні числа тест не зловить. Для мети «не торкається провайдера і не падає» достатньо; точні значення покриті в профільних тестах інструментів.
2. Низька. Окремого юніт-тесту в `test_event_study.py` на нову поведінку немає, лише інтеграційні через `Client`. Покриття достатнє.
3. Інформаційно. Відкриті питання з `docs/changes` (`liquidations_summary` не відрізняє тихе вікно від вимкненого колектора; `positions_risk` без `mark` і `reference_spot`) чесно названі й не чіпалися: вихід за межі кроку правильний.
