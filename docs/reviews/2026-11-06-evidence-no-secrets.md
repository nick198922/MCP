# Рев'ю: ключі API не потрапляють в `evidence`, health і тексти помилок (пункт 162)

Режим C (код). Предмет: незакомічені зміни `src/tradedesk/core/http.py`, новий `tests/test_evidence_no_secrets.py`, `docs/changes/2026-11-06-evidence-no-secrets.md`, позначка пункту 162 у `docs/ideas/IDEA-20261102-standing-improvements.md`.

**Вердикт: approve.** Блокерів немає.

## Що перевірено самостійно

- `make lint`: чисто (ruff check і format).
- `uv run pytest -q tests/test_evidence_no_secrets.py tests/test_core.py`: 183 passed, збігається з числом у `docs/changes/`.
- **Тести ловлять витік без правки.** Прогнав новий файл з підміненим `tradedesk.core.http._header_secrets = lambda h: ()` (стан до правки, через плагін pytest, код не редагував): десятки падінь у `test_http_error_echoing_key_is_scrubbed`, `test_non_json_success_is_scrubbed`, `test_header_keys_are_scrubbed_without_explicit_secrets` для Coinalyze і CoinGecko, напр. `last_detail` у health = `HTTP 400: Invalid API key SECRET-MARKER-xyz-coinalyze`. Отже витік справжній (Coinalyze і CoinGecko підключені в `runtime.py` без `secrets=`, а `health.redact` ловить лише `api_key=`/`api_key:`), а тест не порожній.
- **Заголовки йдуть незамасковані.** `_header_secrets` лише читає `headers`; `_client_args["headers"]` будується з оригінального словника. `_require_key` у тестах перевіряє точне значення заголовка на самому запиті (`api_key`, `x-cg-demo-api-key`, `Authorization: Bearer …`) і відсутність ключа в URL.
- **Ретраї й ключі кешу не зачеплені.** `self._secrets` використовується лише в `_unsecret` (тексти помилок). Ключ кешу (`request_json`, рядок 208) і логіка `_once` (статуси, `Retry-After`, `_unsent_only`) не змінилися; `_retry_after` читає незамасковану відповідь.
- **Звичайний вивід не спотворюється.** Маска застосовується лише до тексту помилок (`_fail` → `scrub`, `snippet`), не до даних `Fetched.data` і не до `Evidence`. Порожній ключ (`Bearer ` без токена, `api_key` не задано) відсіюється порогом 6 символів; у `runtime.py` заголовки з ключами додаються лише за наявності ключа.
- **Інваріант 2:** нових мережевих шляхів немає. **Інваріант 7:** секрети обчислюються в `__init__`, не прив'язані до клієнта, тож переживають `aclose()`/перестворення клієнта.
- Усі виклики `Http(..., headers=...)` у `src/` і `scripts/` переглянуто Grep-ом: окрім трьох провайдерів, лише `User-Agent`/`Accept` (не збігаються з шаблоном) і `X-Dune-API-Key` у `scripts/verify_market_makers_onchain.py`, який тепер теж маскується (бонус).
- Обсяг: `execution/`, колектори, реєстрації не зачеплені; правка мінімальна (одна функція й один рядок у конструкторі).

## Зауваження (не блокують)

1. **Низька.** Шаблон назви заголовка `(key|token|secret|signature|pass)` широкий: `Idempotency-Key`, `X-Bypass-…`, `Sec-WebSocket-Key` теж підпадуть, і їх значення ≥ 6 символів буде замінено на `***` у текстах помилок. Наслідок лише косметичний (у тексті помилки, не в даних і не в запиті), зараз таких заголовків немає. Виправлення не потрібне; за бажання — згадати в докстрінгу `_header_secrets`.
2. **Низька.** Береться лише останнє слово значення (`rsplit(" ", 1)[-1]`). Для схем на кшталт `Authorization: Signature keyId=…,signature=…` або значення з кількома токенами через пробіл буде замасковано лише хвіст. Зараз так ніхто не шле; коли з'явиться такий провайдер, передавати ключі явно через `secrets=`.
3. **Інфо.** У `test_header_keys_are_scrubbed_without_explicit_secrets` перевірка «ключ усе ще надсилається» дивиться на `http._client.headers` (конфіг клієнта), а не на фактичний запит. Сильніше — асертити `req.headers` у `handler`, як це вже робить `_require_key` для трьох провайдерів. Поточне покриття через `_require_key` достатнє.
4. **Інфо.** Пункт 162 у картці вставлено між 153 і 8a, а не в кінець списку. На роботу не впливає.
