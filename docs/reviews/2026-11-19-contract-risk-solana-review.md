# Рев'ю критика (режим C): `contract_risk` для Solana — пункт 408 standing-improvements

Дата: 2026-11-19. Об'єкт: незакомічені зміни в робочому дереві (`src/tradedesk/analytics/contract_risk.py`, `providers/goplus.py`, `tools/onchain.py`, промпти, документи, `tests/test_contract_risk_solana.py`, фікстура `goplus_solana_token_security_2026-11-19.json`, `docs/changes/2026-11-19-contract-risk-solana.md`).

**Вердикт: `request changes`.** Одна блокувальна вада: на Solana `overall` може бути `pass` без жодної симуляції продажу. На EVM-мережах без honeypot.is це прямо заборонено, і на Solana для цього нема підстав. Решта коду акуратна; що виправити, перелічено нижче.

## Що перевірено власноруч

- `make lint`: чисто (ruff check + format).
- `uv run pytest tests/test_contract_risk.py tests/test_contract_risk_solana.py tests/test_honeypot_is.py`: 68 passed.
- Живі виклики з робочого дерева (`scripts/mcp_call.py --local contract_risk`, бо служба на :8000 ще стара й відповідає «unsupported chain»):
  - BERN: `ok=true`, transfer_tax 0.042 (бере гіршу ставку), tax_modifiable fail, lp_locked unknown (у пулах GoPlus 13 USD, у DexScreener 312 тис. USD), top10 0.4584, overall fail;
  - WIF: lp_locked pass 0.964 (сирі дані власним скриптом: Raydium Standard 99.73 % burn на 4.0 млн USD TVL, решта CLMM без `burn_percent`), top10 0.4459 fail, liquidity_to_mcap 0.023 fail.
  - Третій запит GoPlus поспіль отримав 4029. Це підтверджує примітку автора про швидкий ліміт.
- Одиниці у фікстурі: `holders[].percent` — частка (JUP 0.2478 = balance / total_supply до 4-го знака; BERN так само), `burn_percent` — відсотки 0–100 (є значення 100, 99.73, 36, 0.62), `fee_rate` — десятковий дріб (BERN 0.0269 / 0.042). wSOL: `total_supply "0"` і всі `percent "0"`, тож охорона `supply <= 0 → unknown` потрібна й працює.
- Інваріанти:
  - інструмент не кидає: кожне поле розбирається через `_dec`/`_flag`/`isinstance`, `num` повертає лише скінченні числа;
  - мережа лише через `Http` (`get_json` з `check=`, `ttl=0`), `analytics` чисті;
  - `READ_ONLY` на місці, нового інструмента нема, тож `EXPECTED_TOOLS` не змінюється;
  - описи англійською, запис у `docs/changes/` є, `test_clock_bombs` оновлено.
- Кеш: зберігаються лише успішні відповіді. `_cached` → `TTLCache.get_or_fetch`, а виняток (7012, порожній `result`, 4029) не кешується, і `Http` не кешує (`ttl=0`). Тест `test_solana_7012_is_healthy_and_not_cached` це перевіряє: друга спроба йде в мережу, третя бере з кешу.
- «Нема даних ≠ безпечно»:
  - 7012, `result: {}`, `{mint: {}}` і ключ в іншому регістрі дають `ok=false`, `data=None` (параметризований тест);
  - `assess_solana({})` дає 13 × unknown і `overall=unknown`;
  - кожна перевірка має гілку unknown, коли поле відсутнє або не розбирається.
- base58: адреса не переводиться в нижній регістр ні в GoPlus (`params`, пошук у `result`), ні в DexScreener (у провайдері нема `.lower()`). `liquidity_from_pairs` порівнює в нижньому регістрі обидві сторони. Колізія двох mint, що різняться лише регістром, практично неможлива.
- EVM без змін:
  - рефакторинг `_top10_check` → `_top10_verdict` зберігає семантику (ті самі `_is_burn` і `pair` у `pools`, той самий порядок перевірок);
  - `_cached` повторює попередню логіку `token_security`;
  - ключі кешу `("solana", mint)` і `(chain_id, addr)` не перетинаються;
  - EVM-тести проходять.

## Знахідки

### 1. [БЛОКЕР] На Solana `overall` може бути `pass` без симуляції продажу

`SOLANA_NOT_CHECKED` прибирає `honeypot`, `cannot_sell_all`, `cannot_buy` і `holder_sell_fail_rate` з `checks`. Тому `_summary` їх не бачить, і токен із 13 pass отримує `overall="pass"` (тест `test_clean_token_passes_only_with_liquidity_data` це фіксує як норму). Це суперечить правилу, яке діє в цьому ж інструменті:

- В описі інструмента сказано: «When honeypot.is gives no answer … these five checks cannot pass, so `overall` cannot be pass on arbitrum, polygon, optimism or avalanche». У `_merge_check` записано: «one source alone never makes it green». На Solana джерело теж одне, симуляції нема, а зелений вердикт можливий.
- Правило промпта `pre_trade_checklist` «Невідомі honeypot чи cannot_sell_all → ⚠, перевір вручну в 2–3 джерелах» спрацьовує лише через список `unknown`. На Solana ці назви стоять у `not_checked`, тож ⚠ не з'явиться, і план може пройти крок 0.1 зі статусом «✓ pass».
- Обидва промпти пишуть: «`overall=pass` — евристика двох чекерів (GoPlus і симуляція honeypot.is)». Для Solana це неправда.
- Блокування продажу на Solana не зводиться до полів, які читає GoPlus (див. п. 2: розширення Token-2022 Pausable; пул на власній програмі; ліквідність, яку GoPlus не бачить).

Рекомендація: не приймати варіант «pass з попередженням». Потрібна постійна перевірка, яка ніколи не дає pass:
- у `assess_solana` додати `_check("honeypot", "unknown", "critical", None, "buy and sell simulation must succeed", "no buy/sell simulation for Solana (honeypot.is does not cover it, GoPlus Solana has no honeypot flag); token-program powers are checked separately, Token-2022 pause and pool-program blocks are not visible")`;
- прибрати `honeypot` з `SOLANA_NOT_CHECKED`, щоб інваріант «nothing is both scored and not_checked» лишився.

Тоді `overall` на Solana не вище `unknown`, як на arbitrum чи polygon. Вердикти окремих перевірок і `failed` лишаються корисними, а промпт сам вимагатиме ручної перевірки продажу. Ще треба:
- в описі інструмента дописати, що `overall` cannot be pass on solana (no sell simulation);
- у промптах уточнити, що «два чекери» стосуються лише ethereum/bsc/base;
- змінити тести: чистий токен з ліквідністю дає `overall="unknown"`, `unknown == ["honeypot"]`, 14 перевірок; `test_live_jup_classic_spl` і `test_missing_data_is_unknown_never_pass` перерахувати.

Друге джерело для Solana (наприклад, котирування продажу через агрегатор або RugCheck) — окремий наступний пункт, а не умова цього злиття.

### 2. [СУТТЄВА] `not_checked["pausable"] = "covered by freezable and default_account_frozen"` — хибне твердження

Наскільки мені відомо (живцем не перевіряв), Token-2022 з 2025 року має розширення Pausable: окремий pause authority зупиняє всі transfer, mint і burn. Від freeze authority воно не залежить. У схемі GoPlus Solana такого поля нема (у фікстурі ключі: authorities, `transfer_fee*`, `transfer_hook*`, `default_account_state*`, `non_transferable`, `metadata*`, `holders`, `dex`, `lp_holders`, `creators`, `trusted_token`).

Виправлення:
- для `pausable` написати причину «Token-2022 Pausable extension: no field in the GoPlus Solana schema, not checked»;
- для `blacklist` і `whitelist` лишити посилання на freezable;
- у `docs/knowledge/defi.md` пом'якшити фразу «Продаж на Solana може заблокувати лише токен-програма». Корректно так: «серед того, що видно в GoPlus, — …; паузу Token-2022, власну програму пулу й зняття ліквідності GoPlus не показує».

### 3. [ДРІБНА] `malicious_address` в об'єктах authority не використовується

GoPlus кладе `{"address": …, "malicious_address": 0/1}` у кожен authority (і, ймовірно, в `creators`). Тому причина `same_creator_honeypot: "no such field in the GoPlus Solana schema"` неточна.

Виправлення (на вибір):
- (а) red-flag перевірка `malicious_authority`: fail, якщо будь-який `malicious_address == 1` серед authorities і creators; pass лише тоді, коли всі вказані адреси мають явний 0 або authorities порожні й `status "0"`; інакше unknown;
- (б) мінімум — переписати причину в `not_checked` і віддати прапорець в `info`.

### 4. [ДРІБНА] top10: ненульова `total_supply` з нульовими частками дає хибний pass

Охорона ловить лише `total_supply <= 0`. Якщо GoPlus віддасть додатну `total_supply`, але всі `percent "0"` (так само, як у wSOL, але з іншою причиною), `_top10_verdict` порахує top = 0 і поставить pass. Виправлення: у `_sol_top10_check`, якщо сума часток із пулами включно дорівнює 0, повертати unknown. Можна також звіряти `percent` з `balance / total_supply` і брати більше з двох (PYUSD: 0.5788 проти 0.5697). Плюс тест. Та сама вада є на EVM, але там вона давня й не в межах цієї зміни.

### 5. [ДРІБНА] Формулювання в `pre_trade_checklist.md`, рядок 11

«…перелічені в `not_checked` — назви їх «не перевірено»; критичні там — non_transferable і default_account_frozen…» читається так, ніби non_transferable і default_account_frozen лежать у `not_checked`. Насправді вони в `checks`. Після виправлення п. 1 краще так: «на Solana `honeypot` завжди unknown (симуляції нема) → ⚠ і ручна перевірка продажу; критичні перевірки Solana — non_transferable, default_account_frozen; freezable (freeze authority) — головна ознака Solana-скаму».

### 6. [ДРІБНА] Прогалина в тестах

- Нема тесту на Solana-гілку зі збоєм DexScreener (`rows is None`: `lp_locked` рахується лише за пулами GoPlus, `liquidity_to_mcap` unknown, `ok=true`). Треба додати, щоб зафіксувати, що overall тоді не pass.
- Після п. 1 потрібен тест-інваріант: для будь-якого Solana-входу `overall != "pass"`.

## Що не є проблемою (перевірено)

- Код 7012 як «нема даних» з health=ok: якщо GoPlus почне віддавати 7012 на все, кожен Solana-токен стане `ok=false`. Помилка тоді йде в безпечний бік.
- `lp_locked` на Solana консервативний. Pass потрібна відома частка ≥ 0.95 від max(TVL GoPlus, ліквідність DexScreener). Ліквідність поза пулами GoPlus і пули без `burn_percent` не зараховуються. BERN це підтвердив живцем: unknown, а не pass.
- `freezable` як red_flag, а не critical: прийнятно (USDC/PYUSD мають freeze authority), і промпт однаково вимагає пропуску при будь-якому fail.
- Безпека: mint проходить валідацію base58 перед запитом (тест: жодного запиту на невалідну адресу), назва й символ проходять через `clean_text`, текст `message` з GoPlus не відображається.

## Повторне рев'ю (2026-11-19, раунд виправлень)

**Вердикт: `approve`.** Блокер усунено. Знахідки 2, 4, 5 і 6 виправлено. Знахідку 3 закрито мінімальним варіантом: причину переписано, а саму перевірку явно відкладено (див. нижче). Регресій на EVM не знайшов.

### Що перевірено власноруч
- `make lint`: чисто.
- `uv run pytest -q tests/test_contract_risk.py tests/test_contract_risk_solana.py tests/test_honeypot_is.py tests/test_clock_bombs.py`: 79 passed.
- Живий виклик `scripts/mcp_call.py --local contract_risk` для JUP (solana). Результат: 14 перевірок, `honeypot` unknown (critical) стоїть першою, `unknown = [honeypot, lp_locked]`, `failed = [top10_holders, liquidity_to_mcap]`, `overall = fail`. У `missing` два записи (`honeypot`, `evm_only_checks`), у `warnings` є рядок «overall cannot be pass».
- Diff `analytics/contract_risk.py` проти робочого дерева попереднього раунду. EVM-шлях (`assess`, `_merge_check`, `_summary`) не змінено. Рефакторинг `_top10_check` → `_top10_verdict` той самий, що я вже перевіряв, і EVM-тести проходять.

### Стан знахідок
1. **[БЛОКЕР] — виправлено.** У `assess_solana` першою стоїть `_check("honeypot", "unknown", "critical", …)` з текстом, який я рекомендував. `honeypot` прибрано з `SOLANA_NOT_CHECKED`, тож жодна назва не потрапляє одночасно в scored і в `not_checked`. Для `cannot_sell_all`, `cannot_buy` і `holder_sell_fail_rate` причина посилається на цю перевірку. Про те, що на solana `overall` cannot be pass, тепер сказано в описі інструмента, у docstring модуля й функції, у `warnings` відповіді, в обох промптах («два чекери» лише на ethereum/bsc/base), у `defi.md`, README, PARITY, HANDOFF, BACKLOG, картці й `docs/changes/`. Скрізь стоїть «14 перевірок», старого «13» ніде нема.
2. **[СУТТЄВА] — виправлено.** `pausable` має причину «Token-2022 Pausable extension …: no field in the GoPlus Solana schema, not checked». `blacklist` і `whitelist` посилаються на freezable. У `defi.md` написано «серед механізмів, які видно в GoPlus, …; паузу Token-2022, власну програму пулу й зняття ліквідності GoPlus не показує».
3. **[ДРІБНА] — частково, прийнятно.** Причину `same_creator_honeypot` переписано: прапорець `malicious_address` прямо названо таким, що «is not read yet». В `info` його не віддано, а перевірку `malicious_authority` записано наступним кроком у `docs/changes/`. Для злиття це не перешкода: прапорець може дати лише fail або unknown і на `overall` вплинути не може, бо на Solana той уже ніколи не pass.
4. **[ДРІБНА] — виправлено.** `_sol_top10_check` повертає unknown, коли додатна `total_supply` поєднується з тим, що жодна частка не більша за 0. Тест `test_top10_excludes_pools_authorities_and_burns` перевіряє саме цей випадок (`percent "0"` і `""` при `total_supply "1000000"`). Звірку `percent` з `balance / total_supply` не зроблено. Вона була необов'язковою.
5. **[ДРІБНА] — виправлено.** У `pre_trade_checklist.md` рядок про Solana тепер каже: `honeypot` завжди unknown, отже ⚠ і ручна перевірка продажу; критичні перевірки — non_transferable і default_account_frozen; у `not_checked` лежать EVM-перевірки без поля в схемі. Двозначність прибрано.
6. **[ДРІБНА] — виправлено.**
   - Тест `test_contract_risk_solana_dexscreener_failure` (DexScreener 503) перевіряє, що `ok=true`, `lp_locked` береться лише з пулів GoPlus, `liquidity_to_mcap` unknown, `overall=unknown` і в `missing` є dexscreener.
   - Параметризований інваріант `test_solana_overall_is_never_pass` проходить сім входів: чистий з ліквідністю і без неї, порожній, а також чотири живі зразки з ліквідністю 1e9.
   - У `test_clean_token_is_unknown_without_sell_simulation` перевірено, що за 13 pass `overall` дорівнює unknown, а `unknown` дорівнює `["honeypot"]`.

### Залишкові дрібниці (не блокують, виправляти не обов'язково)
- Загальне попередження «pass is not proof of safety» у Solana-гілці звучить зайве, бо pass там неможливий. Його врівноважує наступне попередження, тож нічого хибного воно не каже.
