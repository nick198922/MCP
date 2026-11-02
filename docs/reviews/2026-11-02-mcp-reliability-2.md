# Рев'ю (режим C): надійність MCP, пункт 8: Telegram без дублів, пауза ретраю, проводка `TRADEDESK_HTTP_RETRIES`

Дата: 2026-11-02. Предмет: незакомічений diff робочого дерева (`src/tradedesk/core/http.py`, `alerts/channels.py`, `config.py`, `runtime.py`, `tests/test_alerts.py`, `tests/test_core.py`, `tests/conftest.py`, `docs/changes/2026-11-02-mcp-reliability-2.md`, BACKLOG / HANDOFF / картка standing-improvements).

**Вердикт: request changes.** Код правильний, і я не знайшов у ньому ні дефекту, ні витоку токена. Але дві заявлені властивості тестами не захищені: якщо їх зламати, набір тестів однаково пройде. Обидва виправлення займають кілька рядків у тестах.

## Що перевірено самостійно
- `make lint`: чисто (ruff check + format).
- `tests/test_alerts.py` і `tests/test_core.py`: 40 passed. Окремо прогнано 10 нових випадків: 6 Telegram, 3 проводки env, 1 backoff.
- Мутаційна перевірка скриптом поза репозиторієм (`/tmp/critic_mut/mut_check.py`): підмінив `_retry_after` на «лише заголовок», і тест `…[429-2-True]` однаково проходить (див. п. 1). Для ReadTimeout типовий `Http` (без `retry_unsent_only`) живцем робить 2 виклики, тобто поведінка зараз правильна (див. п. 2). `_retry_after` повертає 7 для `{"parameters": {"retry_after": 7}}` і `None` для тіла-списку та `parameters: null`, тож винятку назовні немає.
- Інші провайдери. Ніде, крім `Runtime` і `channels.py`, `backoff_base` не передається, і скрізь дорівнює 0.4, тому джитер `random()*backoff/4` = 0.1 с, як і раніше. `Exchanges(retry_backoff=0.4)` за замовчуванням теж не змінився. Єдина нова поведінка для всіх: 429/5xx без заголовка `Retry-After`, але з `parameters.retry_after` у JSON-тілі, тепер чекає це значення (максимум 5 с). Це нешкідливо, і `docs/changes` про це пише.
- Секрети. Токен лежить у шляху `/bot<token>/sendMessage`. `Evidence.tool="sendMessage"`, тобто шлях туди не потрапляє. Кешу немає (`ttl=0`). Кожна помилка проходить `Http._fail → scrub → redact`, а там `_BOT_TOKEN` маскує `/bot<id>:<secret>`. Текст помилок httpx для `ConnectError` / таймаутів URL не містить. Нових шляхів, якими токен міг би потрапити в лог, health або таблицю `alerts`, diff не додає.
- Семантика «запит не дійшов». `ConnectError` / `ConnectTimeout` / `PoolTimeout` у httpx/httpcore виникають лише на етапі пулу, TCP або TLS, тобто до відправки байтів запиту. Протухле keep-alive-з'єднання дає `RemoteProtocolError` / `ReadError`, а не `ConnectError`, тож воно правильно не повторюється. 429 від Telegram (flood control) означає, що запит відхилено без обробки. Класифікацію вважаю правильною.
- Інваріанти CLAUDE.md. Мережа йде лише через `Http`. `TelegramChannel.send` ловить усе й повертає `(False, err)`, отже винятків назовні немає. `analytics/` не зачеплено, коду ордерів немає. Записи в `docs/changes/`, BACKLOG і HANDOFF є, і їхній зміст відповідає diff. Обсяг змін відповідає пункту 8, зайвого немає.

## Знахідки

1. **[блокер, середня] Читання `retry_after` з тіла 429 не перевіряє жоден тест.** Тест `test_telegram_send_message_retries_only_when_the_request_surely_did_not_arrive[429-2-True]` ставить `retry_after: 0` і `http_retry_backoff=0.0`. Без розбору тіла код однаково йде на звичайний backoff 0 с і робить ту саму другу спробу. Мутація «`_retry_after` повертає лише заголовок» тест проходить (перевірено). `docs/changes` і коментар у `channels.py` заявляють «honours retry_after», але це не доведено.
   **Виправлення:** додати в `tests/test_core.py` тест на `Http` з обробником `429 {"parameters": {"retry_after": 3}}` → `200`, з `backoff_base=0.0` і підміненим `asyncio.sleep`, який записує паузи. Перевірити `sleeps == [3.0]` і 2 запити. Бажано ще один випадок: `retry_after: 30` → `sleeps == [5.0]` (стеля). Або хоча б параметризований юніт-тест `_retry_after`: заголовок має пріоритет над тілом, значення з тіла, не-JSON, список, `parameters: null`, bool.

2. **[блокер, середня] Твердження «решта провайдерів поводиться як раніше» не захищене для мережевих помилок після відправки.** Жоден тест не кидає `ReadTimeout` / `RemoteProtocolError` / `ReadError` у типовий `Http`: grep по `tests/` знаходить їх лише в новому Telegram-тесті. Якщо з умови `if self._unsent_only and not isinstance(e, _UNSENT): break` випаде `self._unsent_only and` (в одній гілці чи в обох), усі MCP-провайдери тихо перестануть повторювати таймаути читання, і набір тестів цього не помітить. Для 5xx захист є: `test_runtime_wires_http_retries_from_the_environment` очікує 2 запити на 503.
   **Виправлення:** параметризований тест у `tests/test_core.py`: `make_http(handler, retries=1, backoff_base=0)` (або явний `Http(...)`), перша спроба кидає `ReadTimeout` / `RemoteProtocolError`, друга повертає 200. Перевірити 2 запити й успіх. Це дзеркальна пара до Telegram-тесту.

3. **[низька, не блокує] 429 від Telegram із `retry_after` > 5 с.** Пауза обмежена 5 с, тож друга спроба майже напевно отримає новий 429, і алерт буде записано як невдалий, після того як джоба планувальника чекала до ~10 с (2 ретраї × 5 с). Дубля немає, і так поводився й заголовок до цієї зміни. Достатньо згадати це в коментарі або змиритися. Якщо обмеження алертів стане частим, тоді вже думати про чергу.

4. **[низька, не блокує] Telegram-тест можна зробити заодно перевіркою секретів.** Токен у тесті `"1:abc"` під шаблон `_BOT_TOKEN` (`\d+:[A-Za-z0-9_-]+`) підпадає, але `err` на нього не перевіряється. Один рядок `assert "abc" not in (err or "")` закріпив би інваріант «нічого секретного в `alerts`» саме для цього шляху. Додатковий захист: передати `secrets=(settings.telegram_bot_token,)` у `Http("telegram", …)`. Тоді маскування не залежатиме від того, чи збігся регекс з форматом токена.

5. **[інфо]** Тест підміняє транспорт через `channel._client.http._client_args["transport"]`, тобто лізе в приватне поле. Це крихко, але працює, бо клієнт створюється ліниво. Дозволено лишити.

## Що потрібно до approve
Пункти 1 і 2: два невеликі тести. Сам код змінювати не треба.

## Повторне рев'ю

Дата: 2026-11-02. Предмет: коміт `85a9b49` на `main` (`core/http.py`, `alerts/channels.py`, `runtime.py`, `tests/test_core.py` рядки 102–150, `tests/test_alerts.py`).

**Вердикт: approve.** Обидва блокери закрито тестами, і мутаційна перевірка показує, що ці тести справді ловлять поломку.

### Що перевірено самостійно
- `make lint`: чисто. `tests/test_core.py` + `tests/test_alerts.py`: 47 passed.
- Мутаційна перевірка скриптом поза репозиторієм (`~/.cache/tradedesk-agents/tmp/critic_mut2/mut_check.py`). Скрипт копіює `src/` і `tests/` у тимчасову теку й мутує копію `http.py`. Я перевірив, що імпорт іде саме з копії. Робоче дерево репозиторію лишилось чистим (`git status` порожній). Результати:
  - baseline: 22 passed (цільові тести + `test_alerts.py`);
  - M1, `_retry_after` читає лише заголовок: падають `[{}-3]` і `[{}-30]`;
  - M2, прибрано стелю 5 с: падає `[{}-30]`;
  - M3, у гілці `TimeoutException` прибрано `self._unsent_only and`: падає `[ReadTimeout]`;
  - M4, те саме в гілці `HTTPError`: падають `[RemoteProtocolError]` і `[ReadError]`;
  - M5, тіло має пріоритет над заголовком: падає `[Retry-After: 2]`.

### Стан знахідок
1. **Закрито.** `test_http_429_waits_retry_after_from_header_or_body` перевіряє три випадки: значення з тіла, стелю 5 с і пріоритет заголовка. Тест бере `backoff_base=0.0` і записує паузи, тож пауза, відмінна від нуля, можлива лише з `retry_after`.
2. **Закрито.** `test_plain_http_still_retries_errors_after_the_request_was_sent` перевіряє `ReadTimeout`, `RemoteProtocolError` і `ReadError`. Так покрито обидві гілки `except` (`TimeoutException` і `HTTPError`), а в кожному випадку тест очікує 2 запити, успіх і health `ok`.
3. **Прийнято як є** (низька). Стеля 5 с для 429 лишилась. Дублів вона не створює.
4. **Закрито.** У `channels.py` передається `secrets=(settings.telegram_bot_token,)`, а `test_alerts.py:220` перевіряє `"abc" not in err`.
5. Інфо, без змін.
