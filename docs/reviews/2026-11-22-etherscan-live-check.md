# Рев'ю: жива перевірка `onchain_*` (Etherscan V2), `FREE_CHAINS`

Режим C (код). Предмет: незакомічені зміни розробника 22.11, пункт 2б картки [onchain-parity](../ideas/IDEA-20261114-onchain-parity.md): `providers/etherscan.py` (`FREE_CHAINS`, docstring), `tools/evm.py` (`CHAIN_DESC`, `COVERAGE`, опис `onchain_balance`), `tests/test_evm.py` (+5 тестів), фікстура `tests/fixtures/etherscan_v2_live_2026-11-22.json`, `_source` документаційної фікстури, README, PARITY, BACKLOG, HANDOFF, картка, `docs/changes/2026-11-22-etherscan-live-check.md`.

**Вердикт: request changes.** Головне твердження зміни («безкоштовний ключ відкриває лише ethereum, polygon, arbitrum; решта мереж → `ok=false`, `provider_tier`») хибне для `onchain_contract_source`: перевірено живцем, на всіх чотирьох «платних» мережах він відповідає `ok`. Опис інструмента тепер каже моделі не викликати робочий інструмент, а документація повторює хибне правило в шести місцях.

## Що я перевірив сам

- `make lint`: чисто. `uv run pytest -q tests/test_evm.py tests/test_server.py`: 143 passed.
- Ключ: у новій фікстурі нема `apikey` (Grep без урахування регістру; збіги лише в адресах, у полі `_source` є тільки назва змінної `ETHERSCAN_API_KEY`); в усіх `evidence.args` фікстури й моїх живих відповідей `apikey` нема; `missing.detail` — наш сталий текст, сирий текст Etherscan не повторюється.
- Інваріант 1: платна мережа не кидає виняток. Живцем через MCP служби 22.11 09:25 UTC: `onchain_balance` base, `onchain_address_txs` base, `onchain_token_info` bsc → `ok=false`, `missing` з `provider_tier` (у `token_info` по одному записі на кожне з 4 полів). Шлях `provider_tier` на рівні інструмента вже покрито тестом (`tests/test_evm.py:391`).
- Інваріант 2: нового I/O нема, зміни лише в тексті й константі. Інваріант 5: нових інструментів нема; описи англійською; README / PARITY і запис у `docs/changes/` є. Інваріант 6: фікстура з датою й джерелом.
- Обсяг: змінено лише перелічені файли, зайвого нема. Q-20261119-1 у `docs/QUESTIONS.md` уже `answered (22.11)`.
- Живі виклики, які я зробив (`scripts/mcp_call.py`, 22.11 09:25–09:26 UTC):
  - `onchain_contract_source` bsc (USDT `0x55d3…7955`), base (USDC `0x8335…2913`), optimism (USDC `0x0b2c…ff85`), avalanche (USDC `0xb97e…8a6e`): усі чотири `ok=true`, `verified=true`, `missing=[]`.
  - `onchain_token_info` polygon (USDC `0x3c49…3359`): `ok`; `onchain_address_txs` arbitrum (WETH `0x82af…bab1`): `ok`.
  - `onchain_balance` base, `onchain_address_txs` base, `onchain_token_info` bsc: `provider_tier`.

## Зауваження

1. **Висока. Покриття безкоштовного ключа залежить від ендпоінта, а не лише від мережі: `contract/getsourcecode` працює на всіх семи мережах.** Розробник перевірив платні мережі тільки через `account/balance` і узагальнив це на всі чотири інструменти. За моїми викликами `account/*` і `proxy/eth_call` на bsc / optimism / base / avalanche справді дають відмову (`provider_tier`), а `onchain_contract_source` там відповідає `ok`. Наслідки:
   - `COVERAGE` і `CHAIN_DESC` у `tools/evm.py`, які додано й до опису `onchain_contract_source`, кажуть «bsc, optimism, base, avalanche are refused … the answer is ok=false». Модель не викличе робочий інструмент саме на тих мережах (bsc, base), де `contract_risk` найчастіше потрібна звірка відкритого коду (пункт BACKLOG «Перехресна перевірка відкритого коду через Etherscan V2»).
   - Те саме хибне правило записано в docstring і коментарі над `FREE_CHAINS` у `providers/etherscan.py`, у README, PARITY, BACKLOG, HANDOFF, картці й `docs/changes/2026-11-22-etherscan-live-check.md`.

   **Виправлення:** описати покриття за ендпоінтами. `FREE_CHAINS` залишити для `account/*` і `proxy/eth_call` (`onchain_balance`, `onchain_address_txs`, `onchain_token_info`), а для `onchain_contract_source` у `Field(description=…)` і в опис покласти окремий текст: «free key answers on all CHAINS (checked live 2026-11-22)». Це можна зробити як окремі `CHAIN_DESC` / `COVERAGE` або як параметр функції, що будує текст. До живої фікстури додати конверти `onchain_contract_source` для bsc, optimism, base, avalanche (знімаються через `scripts/mcp_call.py`; поле `source_files` — недовірений текст, його можна обрізати). Додати тест на те, що ці конверти `ok`, а опис `onchain_contract_source` не містить слова «refused». Виправити docstring провайдера, README, PARITY, BACKLOG, HANDOFF, картку й запис у `docs/changes/`. У рядку BACKLOG про звірку `contract_risk.open_source` зазначити, що джерело безкоштовне на всіх семи мережах.

2. **Середня. Твердження «polygon і arbitrum працюють для всіх чотирьох інструментів» перевірено лише через `account/balance`.** У фікстурі `chain_balance` для цих мереж є тільки баланс, а описи й документація подають покриття як повне. Мої виклики (`eth_call` на polygon, `txlist` на arbitrum) показують, що це правда, але в артефактах цього не зафіксовано. **Виправлення:** разом із пунктом 1 додати до живої фікстури хоча б один не-балансовий виклик на polygon і на arbitrum (наприклад, `onchain_token_info` на polygon і `onchain_address_txs` на arbitrum) і розширити ними `test_live_free_key_chain_coverage_matches_the_code`. Тоді таблиця «ендпоінт × мережа» в описі спиратиметься на фікстуру.

3. **Низька (довідково, змін не потребує). Тести `test_live_numbers_round_trip_*` наполовину замкнені самі на себе.** Сире тіло будують із вихідних даних інструмента, тож тест ловить лише арифметику масштабування й ABI-декодування, а зміну назв сирих полів Etherscan не зловить. Справжній сигнал дає перевірка «у живих рядках нема `None`» у `test_live_output_shapes_match_the_tools_on_the_documented_bodies`. Docstring тестів і `_source` фікстур про це прямо попереджають, а сирих тіл без ключа агенти зняти не можуть, тому це прийнятно.

## Після виправлення

Повторне рев'ю потрібне лише для пунктів 1–2: перевірю текст описів трьох інструментів проти `onchain_contract_source` і нові записи фікстури.

## Повторне рев'ю (22.11, після фікс-раунду)

**Вердикт: approve.** Блокери 1 і 2 закрито, нових помилок не знайшов.

Що я перевірив сам:

- `make lint`: чисто. `uv run pytest -q tests/test_evm.py tests/test_server.py`: 145 passed (раніше 143; +2 нові тести на покриття коду контракту й на текст опису).
- **Пункт 1 закрито.** `providers/etherscan.py`: `FREE_CHAINS = ("ethereum", "polygon", "arbitrum")` лише для `account/*` і `proxy/eth_call`, нове `FREE_SOURCE_CHAINS = tuple(CHAINS)` для `contract/getsourcecode`; docstring і коментарі кажуть, що покриття залежить від ендпоінта. `tools/evm.py`: я вивів чотири тексти. `CHAIN_DESC` / `COVERAGE` (у balance, address_txs, token_info) називають платні мережі й `provider_tier` і окремо кажуть, що `onchain_contract_source` працює на всіх мережах. `SOURCE_CHAIN_DESC` / `SOURCE_COVERAGE` (лише в `onchain_contract_source`, і в описі, і в `Field` параметра `chain`) перелічують усі сім мереж і не містять «refused», «paid» чи `provider_tier`. Grep по `src/`: `COVERAGE` і `CHAIN_DESC` ужито тільки в трьох інструментах `account/*` / `eth_call`. Тест `test_source_tool_is_not_described_as_refused_on_paid_chains` читає описи через `list_tools` і впаде, якщо джерелу знову підставлять загальний текст.
- **Пункт 2 закрито.** У живій фікстурі є `chain_source`: усі сім мереж `ok=true`, `verified=true`, `missing=[]`, evidence `contract/getsourcecode`, на bsc — USDT `0x55d3…7955`, на решті USDC. Є також `token_info_polygon` (`ok`, 4 × `proxy/eth_call`), `txs_normal_arbitrum` (`ok`, `account/txlist`) і відмови `token_info_bsc_refused` (4 × `provider_tier`) та `txs_normal_base_refused` (1 × `provider_tier`). Ці записи збігаються з моїми незалежними викликами 09:25–09:26 UTC. `test_live_free_key_chain_coverage_matches_the_code` і `test_live_free_key_reads_contract_source_on_every_chain` прив'язують їх до `FREE_CHAINS` / `FREE_SOURCE_CHAINS`, тож зміна будь-якої з констант без нового живого зразка не пройде.
- **Ключ.** У живій фікстурі `apikey` / `API_KEY` трапляються лише в полі `_source`: там назва змінної й фраза «without apikey». Токенів у формі ключа Etherscan (34 символи `[0-9A-Z]`) нема. Тест перевіряє, що в `evidence.args` усіх записів `chain_source` і ethereum-записів нема `apikey`. У середовищі агента ключа нема.
- **Документація узгоджена.** Перевірив README, PARITY, BACKLOG (рядок 5.3 і рядок про звірку `contract_risk.open_source`: «джерело безкоштовне на всіх семи мережах»), HANDOFF, картку (2, 2а, 2б `[x]`), `docs/changes/2026-11-22-etherscan-live-check.md` і `_source` документаційної фікстури. Усюди однаково: код контракту працює на всіх семи мережах, баланс, транзакції й token info — лише на ethereum, polygon, arbitrum. Хибного правила «усі чотири відхиляються» не лишилось.
- Обсяг: змінено ті самі файли, що й у першому раунді, нових нема.

Зауваження:

1. **Низька, змін не потребує.** Опис `onchain_balance` тепер називає нативну монету «ETH, or POL on polygon» і більше не згадує BNB / AVAX. Для безкоштовного ключа це правильно. Якщо ключ колись стане платним, текст доведеться повернути, але тоді однаково треба буде переписати `FREE_CHAINS` і описи.
2. Пункт 3 першого рев'ю (тести round-trip частково замкнені самі на себе) лишається довідковим, як і було сказано.
