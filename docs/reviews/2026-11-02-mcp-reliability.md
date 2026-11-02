# Рев'ю: надійність MCP-інструментів (пункт 7 standing-improvements), 02.11.2026

Режим C (код). Предмет: незакомічений diff робочого дерева: `providers/exchanges.py`, `runtime.py`, `core/http.py`, `collector/scheduler.py`, `tools/system.py`, `tests/test_core.py`, `tests/test_collector.py`, звіт `docs/verification/2026-11-02-mcp-reliability.md`, `docs/changes/2026-11-02-mcp-reliability.md`, рядки в BACKLOG / HANDOFF / картці.

**Вердикт: request changes** (1 блокер, решта не блокує).

## Що перевірено самостійно

- `make lint`: чисто.
- `pytest tests/test_core.py tests/test_collector.py tests/test_server.py`: 100 passed. Плюс 19 файлів, що кидають `ccxt.NetworkError` / `RequestTimeout` / `ExchangeNotAvailable` через FakeEx, і тести `execution_*`: 450 passed.
- Ієрархія ccxt 4.5.85: підкласи `NetworkError` = `DDoSProtection`(→`RateLimitExceeded`), `ExchangeNotAvailable`(→`OnMaintenance`), `InvalidNonce`, `RequestTimeout`. Мапа `Exchange.httpExceptions` і `handle_http_status_code` (текст помилки `"<id> <METHOD> <url> <code> <reason> <body>"`).
- Усі 14 викликів `Exchanges._call` (exchanges.py:146–323) — публічні читання; `public_get` пускає лише `*PublicGet*`; інших викликачів `_call` у `src/` немає. У `execution/` немає ні `Exchanges`, ні `Http`. Повтор не зачіпає неідемпотентних викликів.
- `Http`: 404 → `_Absent` лише за `eff_ttl > 0`; `check` (BodyCheck) стоїть поза `try`, тож помилки тіла не кешуються; single-flight `TTLCache` віддає `_Absent` очікувачам, і ті теж отримують `ProviderError`. Інструменти й далі не кидають назовні: це той самий `ProviderError("data_unavailable")`, який ловить `Builder.guard`. Найбільший TTL серед `get_json`/`post_json` у `providers/` — 900 с (ForexFactory), тож 404 не «залипає» на години. POST із `ttl>0` (Hyperliquid `/info`) 404 не віддає, Telegram `sendMessage` має `ttl=0`, тож зміна його не стосується.
- Секрети: деталь ccxt іде через `redact(...)`, як і раніше; `last_failure` — той самий рядок, що вже був у `last_error`.
- Тести впадуть, якщо прибрати зміни: без кешу 404 `seen["n"]` буде 3, без повтору `venue.calls == 1`, без поля `last_failure` — `AttributeError`.

Не перевірено (артефактів немає, лише текст звіту): живі знімки `system_health` о 09:03 / 09:08, «три виклики `next_week` — один запит», відтворення WebSocket на 360 с.

## Знахідки

1. **[блокер, середня] `is_transient` повторює детерміновані 4xx, а не лише «таймаут, DNS чи з'єднання».** ccxt мапить HTTP 400, 403, 404, 405, 409, 410, 451 (як і 5xx) на `ExchangeNotAvailable` (`Exchange.httpExceptions`). Отже, гео-блок (403 / 451 від Cloudflare чи біржі), 400 на кривий параметр `public_get` і 404 на невідомий endpoint тепер отримують другий запит. Це суперечить і docstring `is_transient` («request timeout, DNS or connection error»), і коментарю «Like Http» (`Http` 4xx не повторює, лише 429/5xx/мережу), і звіту та `docs/changes` («тимчасова мережева помилка»). Повторний удар після 403 від WAF — саме те, чого робити не треба. Тест цей випадок не покриває: `ExchangeNotAvailable("a")` без коду проходить як «транзієнтний».
   **Виправлення:** у `is_transient` для `ExchangeNotAvailable` (але не `RequestTimeout`) не повторювати, якщо в тексті є HTTP-код 4xx, напр. `re.search(r" 4\d\d ", str(exc))` (формат `handle_http_status_code`: `"<id> <METHOD> <url> <code> <reason> <body>"`); 5xx (500/502/503/520–530) лишити. Додати в `test_exchanges_retries_one_transient_network_error_only` кейси `ccxt.ExchangeNotAvailable("okx GET https://www.okx.com/x 403 Forbidden <html>")` і `"... 451 ..."` → `venue.calls == 1`, і `"... 502 Bad Gateway ..."` → `venue.calls == 2`. Виправити формулювання в docstring, звіті (пункт 3) і `docs/changes`.

2. **[не блокує, низька] Нові поля `system_health` не покрито тестом.** Зміну в `tools/system.py` (`last_failure`, `last_failure_ts`) можна прибрати, і жоден тест не впаде, хоча саме через `system_health` звіт пропонує шукати причину збоїв WebSocket. **Виправлення:** у тесті, що викликає `system_health` з планувальником, перевірити наявність обох ключів у `collector[]`.

3. **[не блокує, низька] Проводку в `Runtime` не перевірено.** `retries=min(s.http_retries, 1)` і «`TRADEDESK_HTTP_RETRIES=0` вимикає» тестом не закріплено (тест вимикає повтор, лише передаючи `retries=0` у конструктор `Exchanges`). Побічно: `conftest` створює `Runtime(Settings(http_retries=1, …))`, тож кожна мережева помилка FakeEx у тестах тепер чекає 0.4 с. **Виправлення:** один рядок у тесті: `Runtime(Settings(http_retries=0), …).exchanges._retries == 0`; за бажанням — передавати `retry_backoff` із налаштувань, щоб тестовий рантайм мав 0.

4. **[не блокує, спостережуваність] Повтор, що врятував виклик, не лишає сліду.** Ні health, ні лог не фіксують першу невдалу спробу, тож оцінити, як часто повтор спрацьовує (і чи OKX деградує), неможливо. `Http` поводиться так само, тож це не регресія. **Пропозиція:** `log.info("ccxt %s %s: retry after %s", label, tool, type(e).__name__)`.

5. **[інформація] Обсяг пункту 7.** Картка просила виправити три найчастіші причини; найчастішу (WebSocket Bybit/OKX, 14 збоїв) лише інструментовано (`last_failure`), не виправлено. Звіт про це пише чесно, а в BACKLOG є наступний пункт, тож `[x]` у картці прийнятний, якщо наступний пункт справді візьмуть.

6. **[інформація, поза обсягом] Неідемпотентний POST з ретраями існував і раніше.** `alerts/channels.py:70` створює `Http("telegram", …, retries=settings.http_retries)` для `sendMessage`: після таймауту, коли повідомлення вже доставлено, можливий дубль. Цей diff його не зачіпає (`ttl=0`, кеш 404 не діє). Варто окремим пунктом поставити `retries=0` для `sendMessage`.

## Що потрібно для approve

Знахідка 1: звузити `is_transient` до таймаутів, мережі й 5xx, додати тести на 403/451 (без повтору) і 502 (з повтором), виправити формулювання у звіті та `docs/changes`. Знахідки 2–3 бажано закрити в тому ж кроці: це по одному рядку в тестах.

## Повторне рев'ю (02.11.2026)

**Вердикт: approve.** Блокер 1 закрито, знахідки 2 і 4 закрито, знахідка 3 лишається відкритою (не блокує).

### Що перевірено самостійно

- `make lint`: чисто. `pytest tests/test_core.py tests/test_collector.py tests/test_server.py`: 100 passed; три нові/змінені тести (`test_exchanges_retries_one_transient_network_error_only`, `test_http_404_is_cached_for_the_call_ttl_and_counted_once`, `test_scheduler_keeps_last_failure_after_a_success`) окремо: 3 passed.
- **Знахідка 1.** `is_transient` тепер відсікає `ExchangeNotAvailable`, у тексті якого є `" 4\d\d "`. Перевірив не на рукописних рядках, а на винятках, які породжує сам ccxt 4.5.85: `ccxt.okx().handle_http_status_code(code, …)` для кожного коду з `httpExceptions` плюс порожні reason / body, і `is_transient` на них:
  - без повтору: 400, 403, 404, 405, 409, 410, 451 (`ExchangeNotAvailable`), 429 (у OKX теж `ExchangeNotAvailable`, відсікається тим самим regex), 418 (`DDoSProtection`), 401 (`AuthenticationError`); 403 з порожнім reason і body теж без повтору;
  - з повтором: 500, 501, 502, 503, 520, 530 (`ExchangeNotAvailable`), 408 і 504 (`RequestTimeout`), 502 з порожнім reason / body;
  - транспортні збої async-ccxt (`async_support/base/exchange.py:275–284`: `ExchangeNotAvailable` / `RequestTimeout` з текстом `"<id> <METHOD> <url>"` без коду) і звичайна `NetworkError` повторюються; `ExchangeError` ні. URL у тексті не містить пробілів, тож хибний збіг з кодом у шляху неможливий; збіг у тілі 5xx лише скасовує повтор (так і написано в docstring).
  Тест упаде, якщо прибрати regex: кейси 403 / 451 вимагають `venue.calls == 1`, 502 — `2` і рядок `log.info`. Docstring, звіт (п. 3), `docs/changes` і HANDOFF тепер кажуть «таймаут, з'єднання чи 5xx; 4xx не повторюється», що збігається з кодом.
- **Знахідка 2.** `test_scheduler_keeps_last_failure_after_a_success` тепер викликає `system_health` через `build_server` + `Client` і перевіряє `last_failure` і `last_failure_ts` у рядку `collector[]`: якщо прибрати поля з `tools/system.py`, тест упаде (`KeyError`).
- **Знахідка 4.** Кожен повтор пише `log.info("%s %s: retry %d after %s", …)` у логер `tradedesk.providers`; тест перевіряє точний рядок для 502 і його відсутність для 403 / 451. Деталь винятку в лог не йде (лише назва класу), тож секретів там немає.
- Новий BACKLOG-пункт про `sendMessage` без ретраїв (знахідка 6) і пункт про WebSocket (знахідка 5) додано.
- Решта diff після першого рев'ю: лише regex, логер і тести; `_call`, `Http`, планувальник по суті не змінились. Ширший набір (19 файлів з FakeEx, `execution_*`) не перезапускав: зміна звужує повтор і не торкається шляхів без винятку; повний прогін робить координатор.

### Що лишилось (не блокує)

1. **[низька] Знахідка 3 не закрита.** Проводку `Runtime` (`retries=min(s.http_retries, 1)`) досі не закріплено тестом: тест вимикає повтор лише через конструктор `Exchanges(retries=0)`. І тестовий `Runtime` з `conftest` (`http_retries=1`) чекає 0.4 с на кожну мережеву помилку FakeEx. **Виправлення:** рядок `assert Runtime(Settings(http_retries=0), …).exchanges._retries == 0` і, за бажанням, `retry_backoff` з налаштувань (0 у тестах). Можна окремим дрібним пунктом.
2. **[інформація] 501 Not Implemented повторюється.** Він детермінований, але трапляється рідко й коштує один зайвий запит; міняти не обов'язково.
