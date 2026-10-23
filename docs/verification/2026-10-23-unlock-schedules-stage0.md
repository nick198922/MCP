# Стадія 0: розклади анлоків DefiLlama як безкоштовна історія пропозиції (`dS90`)

Дата: 2026-10-23. Картка: [IDEA-20261023-supply-from-unlock-schedules](../ideas/IDEA-20261023-supply-from-unlock-schedules.md). Скрипт: `scripts/verify_unlock_schedules.py probe`. Кеш: `~/.cache/tradedesk-agents/unlock_schedules/probe/` (лише умови, документація, метадані GitHub і індекси Wayback). Цін і дохідностей не рахували.

**Підсумок: зупинено на D1 за правилом, записаним заздалегідь.** Умови DefiLlama забороняють копіювати матеріали, автоматичний доступ до сайту й програмний доступ до будь-якого API, крім офіційного публічного. Розклади анлоків в офіційному API є лише в платному Pro ($300/міс). Безкоштовний датасет без ключа (`defillama-datasets.llama.fi`) технічно працює, але це внутрішнє джерело сайту, а не офіційний API. Жодного розкладу цей прогін не завантажував. Питання власнику: [Q-20261023-2](../QUESTIONS.md). D2–D5 не перевірялись (◯). Окремо: опублікований 15.10 збіг розкладів із перпами дає орієнтир покриття **≈ 29 %** (206/702, лише розклади з символом; з 73 розкладами без символу ≤ ≈ 40 %; без жодних припущень про зіставлення ≤ 374/702 ≈ 53 %) при порозі D2 50 %. Це орієнтир, а не тест D2 і не верхня межа. Отже, навіть якби умови дозволяли, D2 найімовірніше не пройшов би, але виключити це не можна. D3 не перевірявся (◯), а не провалений.

## Вердикти

| Критерій (записано до перегляду даних) | Результат | Вердикт |
|---|---|---|
| D1 доступ: без ключа, без обходу захисту, умови не забороняють збереження для особистого дослідження | Технічно: `defillama-datasets.llama.fi` віддає дані без ключа (перевірено 15.10, 377 розкладів). Офіційний API: emissions лише в Pro (`pro-api.llama.fi/{KEY}/api/emissions`, `/api/emission/{protocol}`), у списку безкоштовного API (`llms-free.txt`) emissions і unlocks немає; `api.llama.fi/emissions` → 402 (15.10). Умови (Effective Date 24.06.2025, п. 8): заборонено «modify or copy the materials», «transfer or "mirror" the materials on another server», «programmatically access any API other than our official public API», «use robots, spiders or other automated means to access the Site for any purpose». П. 7 дає лише ліцензію на перегляд сайту для особистого некомерційного використання. П. 14: штраф до $100 000 за порушення п. 8 | ✗ (умови забороняють; зупинка, Q-20261023-2) |
| D2 покриття ≥ 50 % криптоперпів Binance/Bybit | не рахувалось (зупинка на D1). Орієнтир зі звіту 15.10 (не тест): 206 із 377 розкладів зіставлено з перпом Binance, Bybit **або** Hyperliquid; універсум D2 — 702 криптобази Binance/Bybit, отже орієнтир 206/702 ≈ 29 %. Це не верхня межа: 73 розклади без символу пропущено ще до зіставлення (`scripts/verify_token_unlocks.py`, рядки 360–363), серед 95 «no_perp» можливі пропуски через перейменування тікерів. З 73 розкладами без символу ≤ (206 + 73)/702 ≈ 40 %; без припущень ≤ 374/702 ≈ 53 % (377 розкладів мінус 3 помилки) | ◯ (n = 0; орієнтир ≈ 29 %, діапазон ≈ 29–53 %) |
| D3 point-in-time: ≥ 20 пар, медіана розбіжності ≤ 10 %, ≤ 20 % пар > 25 % | не перевірявся. Офіційного датованого джерела немає: оригінальний репозиторій `DefiLlama/emissions-adapters` — GitHub 404 (як і 15.10), датасет у Wayback не архівовано (0 копій для `arbitrum`, за префіксом лише `emissionsIndex` і `emissionsProtocolsList`). Можливі джерела, не перевірені в цьому прогоні: (а) 245 сторінок `defillama.com/unlocks/{slug}` у Wayback (код 200, перші копії 31.03.2023–17.10.2026; CDX-запит згорнуто за `urlkey`, тож кількість дат на токен невідома) — це датовані копії розкладів, але матеріали сайту під тими самими умовами; (б) три незалежні копії адаптерів (`Omni-Chain-Protocols` 01.04.2026, `Define101` до 23.05.2024, `0xnirmal` 03.2023; не форки): ліцензію GitHub не визначає (`license: null`), файли в репозиторіях не перевірялись; історію комітів `Define101` (до 05.2024) не дивились. За описом це код TypeScript-адаптерів (частина, ймовірно, читає блокчейн через RPC — у цьому прогоні не перевірено), а не датований набір даних | ◯ (n = 0, не перевірявся) |
| D4 збіг із фактом (`supply_snapshot`, Спірмен ≥ 0.5) | не рахувалось | ◯ (n = 0) |
| D5 вартість ≤ 1 год, ≤ 1 ГБ | не рахувалось. Орієнтир 15.10: 377 поточних розкладів ≈ 8 хв, 491 МБ сирого JSON; історія для D3 додала б кратний обсяг | ◯ |

## Метод

1. Прочитано умови `defillama.com/terms` (`/terms-of-service` віддав Cloudflare 403, обходу не робили), головну сторінку документації `docs.llama.fi`, корінь `api-docs.defillama.com` і машинні списки ендпоінтів `llms-free.txt` та `llms-pro.txt`. Разом шість запитів до доменів DefiLlama: два до `defillama.com`, один до `docs.llama.fi`, три до `api-docs.defillama.com`.
2. GitHub API без ключа (лише метадані): `DefiLlama/emissions-adapters` і `DefiLlama/defillama-server` → 404; пошук `emissions-adapters` знайшов три копії, для жодної GitHub не визначає ліцензію (`license: null`; файли не перевірялись).
3. Wayback CDX (лише індекси): копії датасету й сторінок `/unlocks/`.
4. Розклади не завантажувались. Кешовані 15.10 розклади (`~/.cache/tradedesk-agents/token_unlock/emissions/`, 491 МБ, і `token_unlock/probe/`, 25 МБ, отримані до читання умов) у цьому прогоні не читались і не використовувались. Їхню долю вирішує власник (Q-20261023-2).

## Застереження

- Це прочитання умов агентом, а не юридичний висновок. П. 8 про скрейпінг уточнено словами «for commercial purposes», а заборони копіювати, дзеркалити, ходити роботом і звертатися до неофіційного API такого уточнення не мають. Вирішувати, чи приймати ризик, може лише власник. Як і з CMC, рішення ухвалює він.
- Під час цього прогону скрипт сам зробив шість запитів до доменів DefiLlama: два до `defillama.com` (`/terms` і `/terms-of-service`, другий отримав 403), один до `docs.llama.fi`, три до `api-docs.defillama.com` (корінь, `llms-free.txt`, `llms-pro.txt`). П. 1 умов визначає «Site» як `https://defillama.com/`, тож два запити скрипта до сторінок умов формально підпадають під заборону п. 8 «use robots, spiders or other automated means to access the Site for any purpose». Обсяг мізерний, але це треба назвати прямо. Правило на майбутнє (пропозиція до правил стадії 0 джерел): умови чужого сервісу читати до будь-яких скриптових запитів до його домену, наприклад через копію у Wayback або вручну з участю власника. Скрипт виправлено: `/terms-of-service` прибрано зі списку, невдалі відповіді тепер теж кешуються (маркер `.failed`), а `probe --offline` і `summarize` працюють лише з кешу. Звіт 15.10 ([token-unlock-short-drift](2026-10-15-token-unlock-short-drift.md)) завантажив і використав датасет до того, як умови прочитали. Висновки того звіту від цього не змінюються, але його кеш підпадає під те саме питання.
- Pro API ($300/міс) знімає D1, але не D3: за описом він повертає поточний розклад, історичних версій там немає. Орієнтир покриття (≈ 29 %, діапазон ≈ 29–53 %) від нього теж не залежить.
- Орієнтир D2 має обмеження в обидва боки: збіг за символом станом на 15.10, з Hyperliquid у чисельнику (завищує), без перевірки колізій тікерів (завищує), без 73 розкладів без символу і з можливими пропусками через перейменування (занижує). Це не верхня межа і не оцінка, а лише орієнтир.

## Вивід (скорочено, з ручним аналізом кешу)

Перший блок — вивід `probe` 23.10 (час UTC), скорочений: прибрано `rss=…` і `bytes=…`, текст відповідей обрізано, коментарі в дужках додано вручну. Другий блок — справжній вивід режиму `summarize`, який читає лише кеш і мережею не користується (його додано під час виправлень; рядок `/unlocks` у ньому рахує 246 URL, з них 1 корінь і 245 сторінок токенів).

```
$ uv run python scripts/verify_unlock_schedules.py probe   (23.10, скорочено)
[12:50:00] got defillama_web /terms : 0.030 MB in 0.7s
[12:50:02] FAIL defillama_web /terms-of-service : ProviderError defillama_web: auth: HTTP 403: <title>Just a moment...</title>
[12:50:02] got defillama_docs / : 0.582 MB
[12:50:02] got defillama_apidocs / : 0.003 MB
[12:51:47] FAIL github_api /repos/DefiLlama/emissions-adapters : data_unavailable: HTTP 404
OK  apidocs_llms_free :: '# DefiLlama Free API\n\n> Free API — no authentication required. All endpoints below use base URL https://api.llama.fi'
OK  apidocs_llms_pro  :: '# DefiLlama Pro API (Paid)\n\n> Pro API — requires a paid API key ($300/mo). The pro-only endpoints below are NOT available on the free API.'
[12:51:56] FAIL github_api /repos/DefiLlama/defillama-server : data_unavailable: HTTP 404
OK  gh_search :: {"total_count":15, ...}   (лише 3 репозиторії з назвою emissions-adapters)
OK  wb_ds_arbitrum :: []
OK  wb_ds_prefix :: [["20260930162613",".../emissionsIndex","200"],["20230621173755",".../emissionsProtocolsList","200"]]
OK  wb_unlocks_prefix :: (305 distinct URLs; підрахунок див. summarize нижче)
```

```
$ uv run python scripts/verify_unlock_schedules.py summarize   (лише кеш, без мережі)
apidocs_llms_free: 0 lines matching emission|unlock
apidocs_llms_pro: 6 lines matching emission|unlock
    There are only two tiers: free (no key) and the API plan. One API plan key unlocks every Pro endpoint. There are no add-ons or separate keys for volumes, deriva
    ## Token Unlocks
    ### GET /api/emissions
    Returns: array of {token, sources, protocolId, name, circSupply, circSupply30d, totalLocked, maxSupply, gecko_id, events, nextEvent, unlocksPerDay, mcap}
    ### GET /api/emission/{protocol}
    Unlocks data for a given token/protocol. You can find a list of available slugs to query by querying /emissions and then extracting the key `gecko_id`
gh_search: total_count=15 named emissions-adapters=3 ['0xnirmal/emissions-adapters', 'Omni-Chain-Protocols/emissions-adapters', 'Define101/emissions-adapters']
Omni-Chain-Protocols/emissions-adapters: license=None fork=False created=2026-04-01 pushed=2026-04-01 branch=FelixBruguera-patch-1
Define101/emissions-adapters: license=None fork=False created=2023-04-26 pushed=2024-05-23 branch=master
0xnirmal/emissions-adapters: license=None fork=False created=2023-03-19 pushed=2023-03-17 branch=master
wb_ds_arbitrum: 0 rows []
wb_ds_prefix: 2 rows [['20260930162613', 'https://defillama-datasets.llama.fi/emissionsIndex', '200', '1383601'], ['20230621173755', 'https://defillama-datasets.llama.fi/emissionsProtocolsList', '200', '1225']]
wb_unlocks_prefix: 305 distinct URLs; status 200 without query: 246 (of which root /unlocks: 1, token pages: 245); first captures 20230331..20261017 (collapsed by urlkey)
terms_web: Effective Date 24 June 2025
terms_web clause 8: you agree not to: modify or copy the materials; decompile or reverse-engineer any Content, Services or Subscription Services; remove any copyright or proprietary notices; transfer or "mirror" the materials on another server; use the information for competitive purposes; resell the data or resell access to the data through your plan without permission; republish the data in any form without permission; register duplicate accounts; programmatically access any API other than our official public API; copy, scrape, harvest or otherwise exploit the Content & Data for commercial purposes without prior written consent; interfere with or disrupt the Site, servers or networks; use robots, spiders or other automated means to access the Site for any purpose.
```

Base URL `pro-api.llama.fi/{YOUR_API_KEY}` для двох ендпоінтів Token Unlocks у `llms-pro.txt` стоїть у сусідніх рядках без слів emission/unlock, тому grep їх не показує; це перевірено вручну (і рецензентом).

## Відповідь на рецензію

Рецензія: [2026-10-23-unlock-schedules-stage0](../reviews/2026-10-23-unlock-schedules-stage0.md). Усі шість зауважень перевірено й прийнято, нових мережевих запитів не робили, `probe` повторно не запускали.

1. Покриття: «≤ ≈ 29 %» замінено на орієнтир ≈ 29 % з діапазоном ≈ 29–53 % (≤ ≈ 40 % з 73 розкладами без символу); слова «верхня межа» прибрано. Числа взято зі звіту 15.10 (`schedules 377, error 3, no_symbol 73, no_perp 95, with_perp 206`).
2. D3: скрізь «не перевірявся», з переліком можливих джерел (Wayback-копії `/unlocks/`, копії адаптерів).
3. Запити: шість, з розподілом за доменами; два до `defillama.com` формально підпадають під п. 8.
4. Вивід: блок перейменовано, ручний аналіз замінено справжнім виводом `summarize` (246 = 1 корінь + 245 сторінок).
5. Скрипт: `/terms-of-service` прибрано, невдалі відповіді кешуються маркером `.failed`, додано `probe --offline`. Три невдачі 23.10 маркерів не мають (запущено до виправлення), тому відтворювати слід з `--offline`.
6. Ліцензія й RPC сформульовані як «GitHub не визначає» і «за описом / не перевірено».
