# Рев'ю: EVM-інструменти на Etherscan API V2 (пункт 2 картки onchain-parity)

- **Режим:** C (код). Незакомічені зміни робочого дерева 19.11: `providers/etherscan.py`, `analytics/evm.py`, `tools/evm.py`, `tests/test_evm.py`, фікстура `tests/fixtures/etherscan_v2_docs_2026-11-19.json`, правки `config.py`, `runtime.py`, `tools/__init__.py`, env-прикладів, шести тестів-охоронців і документів.
- **Картка:** [IDEA-20261114-onchain-parity](../ideas/IDEA-20261114-onchain-parity.md), пункт 2 і примітка «Зроблено 19.11».
- **Вердикт:** **request changes**. Два блокери: один у коді (№ 1), другий у процесі (№ 2). Решта коду акуратна, і після цих двох правок зміну можна зливати.

## Що перевірено самостійно

- `make lint`: чисто (ruff check і format).
- `pytest -n 4 tests/test_evm.py tests/test_server.py tests/test_provider_lifecycle.py tests/test_tools_envelope_shape.py tests/test_tools_never_raise.py tests/test_tools_provider_outage.py tests/test_tools_wrong_type_fields.py`: **324 passed**. Повний набір не запускав (його запускає координатор).
- `EXPECTED_TOOLS` має 100 назв, серед них чотири `onchain_*`. `coin_snapshot`, `contract_risk` і `dex_pairs`, на які посилаються описи, існують.
- Живцем, без ключа, з сервера через `core.http.Http`: `account/balance` і `proxy/eth_call` обидва відповідають `{"status":"0","message":"NOTOK","result":"Missing/Invalid API Key"}`. Твердження фікстури й `api-notes.md` (позначка [V]) підтверджено.
- Окремим скриптом (поза репозиторієм) перевірив кешування відповідей `proxy/eth_call` при `cache_scale=1.0`, див. № 1.
- Код прочитано повністю: провайдер, аналітика, інструменти, `core/http.py` (секрети, `_clean_args`, `scrub`, кеш, `check`), `server.configure_logging`, `deploy/tradedesk-mcp.service`.

## Інваріанти й безпека

| Пункт | Стан |
|---|---|
| 1. Інструмент не кидає виняток | ✓ Усі виклики провайдера йдуть через `b.guard`. Розбір в `analytics/evm.py` терпить хибні типи (`fromtimestamp` і `json.loads` загорнуто в try, `uint_text` / `hex_uint` / `abi_string` повертають None). Охоронці never_raise / outage / wrong_type_fields проходять. |
| 2. Мережа лише через `Http` | ✓ Власний `Http("etherscan", …)` з лімітом 2/с і burst 1, кешем і health. |
| 3. `analytics/` чиста | ✓ Без I/O і без годинника. |
| 4. Одиниці | ✓ Ціле в найменших одиницях віддається точним рядком (`*_raw`, `*_wei`), float поруч лише для читання. Нативні 18 decimals для ETH, BNB, POL і AVAX правильні. `fee = gasUsed × gasPrice`, і в описі є застереження про L1 data fee на L2. Decimals понад 255 відкидаються. |
| 5. Чек-лист інструмента | ✓ Описи англійською, з одиницями й застереженнями. Є `Annotated[…, Field]`, `READ_ONLY`, `EXPECTED_TOOLS`, тест happy-path і тест збою. README, PARITY, `docs/changes/` оновлено. |
| 6. Провайдер | ≈ Повертає `Fetched`, підключений у `Runtime`, тести на `MockTransport`. Зразок **документаційний, не живий**: відхилення від «реального зразка», але причину (в агентів нема ключа) названо. Пункт картки свідомо лишається відкритим, доки зразок не замінять живим. Як тимчасовий стан приймаю. |
| 7. Переживає `aclose()` | ✓ `etherscan.http.aclose()` додано в `Runtime.aclose`, і `test_provider_lifecycle` покриває новий провайдер. |
| 8. Ризик-нейтральні ймовірності / GEX | — Не стосується. |
| Ключ не потрапляє в evidence / помилки / логи | ✓ У `evidence.args` ключа немає (`args=` передається без нього, а `_clean_args` і так відкинув би `apikey`). У тексті помилок ключ вирізають два шари: `secrets=` і regex `api[_-]?key=` у `health.redact`. INFO-логи httpx з URL приглушено в `configure_logging`. Ключ є лише в ключі кешу в пам'яті, який нічим не виставляється назовні. `Settings.etherscan_api_key` має `repr=False`. |
| Адреса перевіряється до запиту | ✓ `fullmatch` у `_prepare`, а також повторно в провайдері. Тест перевіряє, що на невалідний ввід не йде жодного запиту, зокрема для адреси з `\n` усередині. |
| Недовірений текст | ✓ Помилки Etherscan зіставляються з відомими фразами й не повторюються. Назви, символи, імена файлів і функцій проходять `display_text` (без Cc/Cf/Co/Cs, з обрізанням довжини). Уривок коду лише на запит, до 4000 символів, з попередженням. |
| `status:"0"` | ✓ «No transactions found» з `[]` означає порожню сторінку. Решта класифікується як rate_limited / auth / provider_tier / backend_error і не кешується. Для **proxy** є виняток, див. № 1. |

## Знахідки

### 1. Блокер. Транзитна помилка `proxy/eth_call` кешується як успіх (до 1 год) і видається за «не ERC-20»

`_proxy_check` (providers/etherscan.py:98–107) пропускає як успішну будь-яку відповідь JSON-RPC з полем `error` і будь-який `result`. `Http` кешує тіло, яке пройшло `check`, і записує health як ok. Тільки після цього `eth_call` / `erc20_view` перетворює його на `data_unavailable`. TTL для name / symbol / decimals становить 3600 с, для totalSupply 60 с.

Перевірив скриптом при `cache_scale=1.0` (типове значення в проді): перша відповідь `{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"header not found"}}` або `{"jsonrpc":"2.0","id":1,"result":"Max rate limit reached"}`, далі нормальна. **Обидва виклики дають `data_unavailable`, запит один.** Друга нормальна відповідь так і не запитується. Для module-формату (`status:"0"`, NOTOK) усе правильно: помилка не кешується, і другий виклик повертає 6.

Наслідок: після одного збою вузла Etherscan (або обмеження, повернутого у форматі JSON-RPC) `onchain_token_info` справжнього токена годину каже «not an ERC-20 view, or reverted». `onchain_balance(token=…)` годину віддає `balance: null` без decimals. Для інструмента, який служить для due diligence токена, хибний сигнал «це не ERC-20» гірший за чесний «збій».

**Виправлення:** у `_proxy_check` вважати успіхом лише дві відповіді:
- `result`, що відповідає шаблону `0x[0-9a-fA-F]*`;
- `error`, чий `message` містить `revert`.

Будь-яка інша `error` (і `result`, що не є hex) має давати `("backend_error", "eth_call failed upstream")`, або `rate_limited`, якщо текст містить «rate limit», через `_classify`. Тоді такі відповіді не кешуються, і health їх бачить. Додати в `tests/test_evm.py` тест із `cache_scale=1.0`: перша відповідь із транзитною `error`, друга нормальна. Другий виклик має повернути значення й зробити другий запит. Окремий тест має підтвердити, що revert і далі дає `data_unavailable`.

### 2. Блокер (процес). Рішення власника щодо ключа не винесено в `docs/QUESTIONS.md`

У проді всі чотири інструменти відповідають `missing: config`. `deploy/tradedesk-mcp.service` читає лише `/etc/tradedesk-mcp.env` і має `ProtectHome=yes`, тож `~/.config/tradedesk/onchain.env` служба не побачить навіть теоретично (перевірено). Рішення про ключ є рішенням власника, і за CLAUDE.md питання до власника пишуться в `docs/QUESTIONS.md`. Зараз воно лише в HANDOFF, картці й changes, і власник може його не побачити. Тим часом чотири нові інструменти мертві, хоча інвентаризація MCP (Q-20261114-1) якраз прибирає зайве.

**Виправлення:** додати `Q-20261119-…` у `docs/QUESTIONS.md`: чи класти `ETHERSCAN_API_KEY` у `/etc/tradedesk-mcp.env`. Вказати, що ключ безкоштовний, той самий, що вже лежить у `onchain.env`, і що він ділиться лімітом 3/с зі скриптами дослідження. Бажано (не обов'язково для злиття) додати скрипт `scripts/server/etherscan-key.sh` за зразком `scripts/server/coinalyze-key.sh`: ключ із `onchain.env` або з прихованого вводу, перевірка запитом, запис через `sudo` від `nick`, перезапуск служби. Тоді в питанні буде одна коротка команда для власника.

### 3. Середня. Тести не покривають кешування

Усі тести Etherscan (і охоронці) працюють при `cache_scale=0.0`, тому знахідку № 1 жоден тест не міг зловити. **Виправлення:** тест із № 1 закриває цю дірку. Окремо додати ще один: «No transactions found» кешується як порожня сторінка (30 с), а `status:"0"` NOTOK не кешується.

### 4. Низька. `onchain_balance(token=<гаманець або не-ERC-20>)` дає `ok=true` з нулем

Для адреси без коду `account/tokenbalance` поверне `"0"`, а decimals і symbol впадуть у `missing`. Відповідь вийде `ok=true, balance_raw="0"` з попередженням лише про decimals. **Виправлення:** якщо і `decimals`, і `symbol` дали `data_unavailable`, додати попередження «token address does not look like an ERC-20 contract; the zero balance is not meaningful». Злиття це не блокує.

### 5. Низька. `rate_limited` у тілі HTTP 200 не ретраїться

Ключ ділиться зі скриптами дослідження. `onchain_token_info` робить 4 виклики, `onchain_balance(token)` робить 3. Якщо скрипт у цей момент теж бере ліміт, частина полів упаде з `rate_limited` без повтору, бо ретраї `Http` діють лише на HTTP 429/5xx. **Виправлення (за бажанням):** один повтор у провайдері через ~1 с на `rate_limited` із тіла. Або хоча б фраза в описах: «on rate_limited, retry in a few seconds». Злиття не блокує.

### 6. Інформаційно. Документи узгоджені

README (рядок «On-chain / EVM»), PARITY (рядок etherscan і лічильник 100), HANDOFF, BACKLOG 5.3 і підпункт `contract_risk`, `api-notes.md`, `.env.example`, `deploy/env.example` і `docs/changes/2026-11-19-etherscan-onchain-tools.md` між собою не суперечать. Пункт 2 картки правильно лишається `[ ]`: умову закриття записано (ключ у службі, жива перевірка, живий зразок, які мережі відкриває безкоштовний ключ). Топ-холдери обґрунтовано відкладено як платний Pro. Файлів поза темою зміна не торкається, коду ордерів немає.

## Що треба для approve

1. № 1: `_proxy_check` приймає лише hex-`result` або revert-`error`, плюс тест із `cache_scale=1.0`.
2. № 2: питання в `docs/QUESTIONS.md` (скрипт `scripts/server/etherscan-key.sh` бажаний).
3. № 3 закривається тестом із № 1 разом із тестом кешування порожньої сторінки.

№ 4 і № 5 можна взяти в цьому ж кроці, але злиття вони не блокують.

## Повторне рев'ю (19.11, після правок)

**Вердикт: approve.**

Перевірено самостійно: `make lint` чисто; `pytest tests/test_evm.py`: 48 passed (інші набори не запускав).

1. **№ 1 виправлено по суті.** `_proxy_check` (providers/etherscan.py:103–120) пропускає лише hex-`result` (`fullmatch 0x[0-9a-fA-F]*`) або `error` з «revert». Усе інше (інша `error`, не-hex `result`, `status:"0"`, відсутні поля, не-dict) дає `backend_error` або `rate_limited`, не кешується й потрапляє в health. Тест `test_transient_eth_call_failure_is_not_cached` працює при `cache_scale=1.0` для трьох хибних відповідей: перший виклик падає з правильною причиною, другий отримує значення, провайдер опитано 3 рази (збій перезапитано, добру відповідь узято з кешу). `test_revert_on_eth_call_is_still_data_unavailable_and_cached` підтверджує, що revert і далі `data_unavailable` і кешується. `test_proxy_check` покриває крайові випадки.
2. **№ 2 закрито.** `Q-20261119-1` у `docs/QUESTIONS.md` з контекстом, трьома варіантами, рекомендацією й однією командою; лічильник відкритих питань оновлено. Скрипт `scripts/server/etherscan-key.sh` прочитано повністю:
   - ключ береться з `onchain.env` і валідується `^[A-Za-z0-9]{20,64}$`, тож ін'єкція в curl-конфіг і `printf` неможлива;
   - до curl ключ іде через stdin (`-K -`), не через аргументи; у вивід потрапляють лише фіксовані рядки; тіло відповіді Etherscan не друкується;
   - запис лише в `/etc/tradedesk-mcp.env`: тимчасовий файл `mktemp` (за замовчуванням 0600), `chown root:tradedesk` і `chmod 640` до `mv`, тож вікна з широкими правами немає; решта рядків env зберігається, старий рядок ключа замінюється;
   - при невдалій перевірці ключа нічого не змінюється; `set -euo pipefail`, `|| true` стоять там, де потрібно.
   Зауваження без блокування: якщо скрипт перерветься між `mktemp` і `mv`, лишиться тимчасовий файл із ключем (root, 0600): безпечно, але його варто прибрати.
3. **№ 3 закрито:** тест `test_empty_page_is_cached_and_status_0_is_not` (кеш порожньої сторінки, NOTOK не кешується, 2 запити з 3 викликів) плюс тести з № 1.
4. **№ 4 взято:** попередження «does not look like an ERC-20 contract» є в tools/evm.py:99, а тест перевіряє, що в транзитному збої воно не з'являється.
5. **№ 5 не взято** (за бажанням, не блокує).

Нових зауважень, що блокують злиття, немає. Після запуску скрипта власником лишається відкритою умова картки: жива перевірка й заміна документаційного зразка живим.
