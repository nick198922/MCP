# Рев'ю C: перепідключення стрімів ліквідацій (2026-11-09)

Предмет: незакомічений diff будівельника: `src/tradedesk/providers/liquidations.py`, `src/tradedesk/collector/scheduler.py`, `src/tradedesk/tools/system.py`, `tests/test_liquidations.py`, документи (пункт 13 картки [standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), `docs/BACKLOG.md`, `docs/HANDOFF.md`, [`docs/changes/2026-11-09-liquidation-stream-reconnect.md`](../changes/2026-11-09-liquidation-stream-reconnect.md)).

## Вердикт: `request changes`

Є один блокер: скидання backoff не працює за типового вікна 60 с. Решта зроблена якісно: скасування в планувальнику коректне, інваріанти дотримано, тести без мережі.

## Що перевірено самостійно

- `make lint`: чисто (ruff check + format).
- `uv run pytest -q tests/test_liquidations.py tests/test_network_boundary.py`: 39 passed.
- `uv run pytest -q tests/test_collector.py tests/test_logfmt.py tests/test_server.py -k "collector or scheduler or health or logfmt or stop"`: 26 passed.
- Окремий скрипт рецензента (не в репозиторії), щоб перевірити:
  - `Scheduler.stop()`, коли задача заблокована всередині `job.fn`, завершується чисто (`_tasks == []`);
  - пряме `task.cancel()` без `_stop` теж завершує задачу (`cancelled=True`);
  - за `max_seconds == stable_seconds` `consecutive_failures` не скидається (див. 1).
- Прочитано вручну: інваріанти CLAUDE.md, обробка `CancelledError`, логіка backoff і пульсу, `system_health`.

## Знахідки

1. **Блокер: за вікна ≤ `STABLE_SECONDS` лічильник збоїв поспіль не скидається ніколи, і кожен прогін починається з паузи.** `consecutive_failures` обнуляється лише тоді, коли з'єднання прожило `stable_seconds` (60 с) усередині одного прогону. Типове вікно в коді й у `deploy/env.example`: `collect_liquidations_window_seconds = 60`. З'єднання відкривається після паузи й рукостискання, тому 60 с до дедлайну воно не проживе ніколи. Після першого ж збою лічильник лишається ≥ 1 назавжди, до рестарту служби. Наслідки:
   - (а) **кожен** наступний 60-секундний прогін спершу чекає `backoff_delay(n)`. Стеля тут `min(60, 60/2) = 30` с × [0.5, 1), тобто після серії з 5–6 збоїв це 15–30 с, і з кожного вікна випадає 25–50 % даних. Це гірше, ніж до зміни, коли розрив після обриву тривав 5 с;
   - (б) `reconnects` росте на кожному прогоні, хоча обриву не було, тож лічильник у `system_health` бреше.

   Мій скрипт (вікно 0.6 с, `stable_seconds` 0.6 с, старт з `consecutive_failures=3`) дав три прогони поспіль з `consecutive_failures 3` і `reconnects` 1 → 2 → 3. На сервері `deploy/features.env` задає вікно 600 с, і там скидання спрацює, якщо `/etc/tradedesk-mcp.env` не перебиває це значення (раніше вже перебивав інші параметри ліквідацій). Але код має бути коректним і на власних типових значеннях.

   **Виправлення:** вважати стабільним і з'єднання, що дожило до кінця вікна без збою (обнуляти `consecutive_failures` перед `return` на дедлайні), та/або брати `stable = min(stable_seconds, max_seconds / 2)`. Додати тест: вікно ≤ `stable_seconds`, `StreamStats(consecutive_failures=3)`, тихе з'єднання до дедлайну. Після прогону очікується `consecutive_failures == 0`, а на другому прогоні `reconnects` не росте. Наявний `test_stream_sends_heartbeat_when_quiet_and_resets_backoff_once_stable` цей випадок не ловить: у ньому `stable_seconds = 0.05` при вікні 0.3. У `docs/changes/…` фраза «скидається, коли з'єднання протрималось 60 с» після виправлення має відповідати поведінці.

2. **Середня: обриви стріму більше не видно в метриках планувальника, а інструкція 8a досі посилається на них.** Тепер прогін падає лише тоді, коли за все вікно не відкрилося жодного з'єднання. Обриви keepalive чи reset посеред вікна (саме ті ~260 випадків із журналу) ідуть у `collector[].stream.last_failure`, а не в `collector[].last_failure`. Пункт 8a картки й рядок BACKLOG «Збої WebSocket-ліквідацій» кажуть читати `collector[].last_failure`, тож після деплою там буде порожньо, і висновок «збоїв немає» буде хибним. Те саме стосується персистентної відмови підписки (`StreamError` одразу після кожного з'єднання): прогін рахується успішним із 0 рядків. Раніше він теж був «успішним», тобто регресії немає, але й видно цього лише в `stream`.

   **Виправлення:** у пункті 8a і в BACKLOG замінити на `collector[].stream.failures` / `stream.last_failure` / `last_failure_ts`. За бажанням: якщо у вікні не було жодного рядка, а кожне з'єднання закінчилося `StreamError`, піднімати останню помилку, щоб прогін рахувався збоєм.

3. **Низька: `except Exception` у `_ws_consume` охоплює і парсер.** Виняток парсера (наприклад, `event.get` на JSON-верхівці, яка не є dict, для Binance) тепер не валить прогін, а стає «збоєм з'єднання» з перепідключенням і backoff. Наявні парсери захищені `try/except`, тож ризик малий, але справжній баг розбору маскувався б під мережевий.

   **Виправлення:** ловити мережеві й протокольні винятки (`OSError`, `TimeoutError`, `websockets.exceptions.WebSocketException`, `StreamError`), а решту пропускати. Інший варіант: винести виклик `parser` за межі `try`.

4. **Низька: недовірений текст біржі без обмеження довжини.** `ret_msg` / `msg` з відповіді біржі потрапляють у `StreamStats.last_failure`, у лог і в `system_health` без обрізання. **Виправлення:** обрізати до ~200 символів у `_bybit_error` / `_okx_error` (або в `last_failure`).

5. **Низька (тести): немає тесту на зупинку планувальника, поки задача висить у `job.fn`.** Саме цей шлях змінено (`task.cancelling()`). Я перевірив його вручну, він працює, але в `test_scheduler_survives_a_stray_cancelled_error` задача повертається одразу, і скасування майже напевно приходить у `wait_for(self._stop.wait())`, а не в `job.fn`. **Виправлення:** додати тест із `fn`, що чекає `asyncio.Event().wait()`. Перевірити, що `stop()` завершується за обмежений час, `_tasks == []` і `failures == 0`. Другий варіант того самого тесту: пряме `task.cancel()` без `_stop`.

## Що в порядку (перевірено читанням)

- **Обробка `CancelledError` у планувальнику.** `stop()` спершу ставить `_stop`, потім скасовує задачі. У `_run_job` виняток прокидається далі, якщо `_stop` встановлено або `task.cancelling() > 0`, тож шлях `wait_for(_stop.wait())` поза `try` не змінився. Python ≥ 3.11, тому `cancelling()` доступний. Сторонній `CancelledError` записується як збій, після чого задача робить звичайну паузу `period_seconds`, тобто тугого циклу немає.
- **`_ws_consume`.** `CancelledError` і `GeneratorExit` (`BaseException`) не перехоплюються `except Exception`, тому `aclose()` і скасування проходять, а `finally` скидає `connected`.
- **Перша спроба прогону.** Вона відбувається завжди: `opened or last_exc` хибне, пауза ≤ половини вікна. Помилка піднімається лише без жодного відкритого з'єднання.
- **Пульс.** OKX отримує `"ping"`, Bybit отримує `{"op":"ping"}` після 20 с тиші. `"pong"` OKX не є JSON, тому пропускається після оновлення `last_message_ts`. Pong Bybit має `success: true`, тож `_bybit_error` його не чіпає.
- **Інваріанти.**
  - У провайдері немає настінного годинника: використовуються `loop.time()` (монотонний, як і раніше) та `clock`, який колектор передає як `rt.now`. Лямбда читає `rt.now` під час виклику, тож підміна годинника діє.
  - `system_health` не кидає: `as_dict()` повертає лише JSON-примітиви, поля `clock` у виводі немає.
  - Одиниці: часові мітки в epoch-секундах, задокументовано.
  - `execution/` не змінено, ключів і секретів немає, URL у логах публічні.
  - Мережа: WebSocket, як і раніше, лише в провайдері, виняток зафіксовано в `tests/test_network_boundary.py`.
- **Документи.** Запис у `docs/changes/` є, HANDOFF і BACKLOG оновлено, пункт 13 картки чесно лишається відкритим: справжні це дірки чи тихий ринок, не виміряно.
- **Обсяг.** Змін поза темою немає.

## Що потрібно до approve

Виправити п. 1 разом із тестом і оновити посилання з п. 2. Пункти 3–5 бажані, але не блокують.

## Повторне рев'ю (2026-11-09)

### Вердикт: `approve`

### Що перевірено самостійно

- `make lint`: чисто (ruff check: All checks passed; format: 3586 files already formatted).
- `uv run pytest -q tests/test_liquidations.py tests/test_network_boundary.py`: 44 passed (було 39, додано 5 тестів, один із них параметризований на 2 випадки).
- Прочитано `git diff` для `providers/liquidations.py`, `tests/test_liquidations.py`, `collector/scheduler.py` (з минулого рев'ю не змінювався), `docs/BACKLOG.md`, пункти 13 і 8a картки, `docs/HANDOFF.md`, `docs/changes/2026-11-09-liquidation-stream-reconnect.md`.

### Стан знахідок

1. **Блокер: виправлено.** У `_ws_consume` на гілці `remaining <= 0` перед `return` стоїть `st.consecutive_failures = 0`. Тихе з'єднання з пульсом теж доходить до цієї гілки: після `TimeoutError` пульс не надсилається, коли дедлайн минув, і `continue` веде на перевірку `remaining`. Новий тест `test_stream_up_to_the_end_of_a_short_window_resets_backoff` бере вікно 0.1 с, `stable_seconds` 0.6 с і старт з `consecutive_failures=3`, робить два прогони й очікує `(connects, reconnects, failures) == (2, 1, 0)`. Без виправлення цей тест упав би: лічильник лишився б 3, а `reconnects` дорівнював би 2. Тест на реконект тепер теж перевіряє `consecutive_failures == 0` наприкінці вікна. Формулювання в `docs/changes/` відповідає поведінці.
2. **Виправлено.** Пункт 8a картки, рядок BACKLOG і `docs/changes/` тепер посилаються на `collector[].stream.failures` / `stream.last_failure` / `stream.last_failure_ts` і пояснюють, що в `collector[].last_failure` лишаються лише прогони без жодного з'єднання. Необов'язкову частину (вважати прогін збоєм, коли кожне з'єднання закінчилось `StreamError` і рядків нуль) не зроблено. Це прийнятно: регресії немає, а `stream.failures` таку ситуацію показує.
3. **Виправлено.** `except` тепер ловить лише `(OSError, TimeoutError, WebSocketException, StreamError)`. Помилка `ws.send` під час пульсу (`ConnectionClosed`) теж потрапляє в цей кортеж. Тест `test_stream_parser_bug_is_not_counted_as_a_dropped_link` перевіряє, що `KeyError` з парсера валить прогін, `failures == 0`, а `connected is False`.
4. **Виправлено.** `_clip` до `ERROR_TEXT_MAX = 200` у `_bybit_error` / `_okx_error`, тест `test_venue_error_text_is_clipped`. Інші винятки в `last_failure` не обрізаються, але причина закриття WebSocket за протоколом не довша за 123 байти, а текст `InvalidStatus` формує бібліотека, тож межа практично є.
5. **Виправлено.** Тест `test_scheduler_cancel_while_a_job_is_running_exits_cleanly[stop|cancel]` чекає, поки `fn` зайде в `asyncio.Event().wait()`, тож скасування приходить саме всередину `job.fn`. Перевіряються обидва шляхи: `stop()` (`_tasks == []`) і пряме `task.cancel()` без `_stop` (`task.cancelled()`), в обох `failures == 0`.

### Зауваження (не блокує)

- Якщо після серії збоїв з'єднання відкрилось за мить до дедлайну й дожило до кінця вікна, лічильник теж скидається, і наступний прогін при мерехтливому з'єднанні почне backoff з 1 с. Наслідок лише в коротшій паузі, даних це не втрачає. Окремо виправляти не треба. Якщо `stream.failures` після деплою покаже часті короткі з'єднання, тоді варто перейти на `stable = min(stable_seconds, max_seconds / 2)`.
- Кодова частина пункту 13 закрита. Питання, чи паузи Bybit/OKX були справжніми дірками, чесно лишається відкритим до звірки на сервері.
