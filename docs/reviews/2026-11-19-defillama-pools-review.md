# Рев'ю коду: `yield_pools`, `yield_pool_history` (DefiLlama yields), 19.11

Режим C. Предмет: незакомічені зміни в клоні: `src/tradedesk/analytics/yields.py`, `src/tradedesk/tools/yields.py`, `providers/defillama.py`, `runtime.py`, `tools/__init__.py`, `tests/test_yields.py` + `tests/fixtures/defillama_yields_sample_2026-11-19.json`, `EXPECTED_TOOLS` і три загальні набори тестів, README, PARITY, `api-notes.md`, картка onchain-parity (п. 5), HANDOFF, BACKLOG, `docs/changes/2026-11-19-defillama-pools.md`.

## Вердикт: **request changes**

Один блокер (порушення інваріанта 1, відтворено). Решта коду акуратна. Після виправлення блокера й тесту на нього можна схвалювати без повторного повного рев'ю.

## Що перевірив сам

- `make lint`: чисто.
- `uv run pytest -q tests/test_yields.py tests/test_server.py tests/test_provider_lifecycle.py tests/test_tools_envelope_shape.py tests/test_tools_never_raise.py tests/test_tools_provider_outage.py tests/test_stablecoin_supply.py`: 137 passed.
- `EXPECTED_TOOLS` = 102, як і пише `docs/changes/…` (CLAUDE.md каже «104»: це розбіжність, що була до цієї зміни, тут не рахую).
- Живцем з клону (`scripts/mcp_call.py --local`): `yield_pools {"stablecoin_only": true, "chain": "eth", "limit": 3}` дав `ok=true`, `pools_total` 15 233, `pools_matched` 500, APY десятковими (sUSDS 0.038, sUSDe 0.0536). `yield_pool_history` для першого пулу дав 637 точок з 2025-02-21. Це збігається з тим, що розробник написав у `docs/changes/`. Позначка «фікстура синтетична, назви полів звірено живцем» чесна: `_note` у фікстурі, README, PARITY, `api-notes.md` [V] і картка кажуть те саме.
- Інваріант 2: уся мережа йде через `Http` (`defillama:yields`, 1 запит/с). `/pools` береться з `ttl=0`, тож у кеші `Http` тіла немає (`request_json`: `eff_ttl <= 0` → без кешу). Провайдер кешує урізані рядки у власному `TTLCache(max_items=2)` на `600 × cache_scale` с. Помилки не кешуються (`get_or_fetch` пробрасує виняток, нічого не кладе). При влучанні в кеш `evidence.cached=true` (є тест).
- Інваріант 3: `analytics/yields.py` без I/O, `now` передається параметром.
- Інваріант 4: усі `apy*`, `apyPct*` і `il7d` діляться на 100. Тест на округлених числах (0.046, −0.004, −0.0035) упав би, якби ділення не було.
- Інваріанти 5 і 6/7: `READ_ONLY`, `Annotated[…, Field(description=…)]`, описи англійською з одиницями й застереженнями (APY оголошений, `apy_reward` у токенах, без IL, газу й комісій, недовірені назви, `contract_risk`). Є happy-path і збій провайдера для обох інструментів, аргументи в `never_raise` / `provider_outage` / `envelope_shape`, клієнт `defillama:yields` у `test_provider_lifecycle`. `aclose()` закриває й клієнт yields, а кеш пулів його переживає.
- Pool id: регулярний вираз UUID перевіряється і в інструменті, і в провайдері до того, як id стає сегментом шляху. Тест показує, що `../pools`, `lido`, id з зайвим символом і порожній рядок не роблять жодного запиту.
- Недовірений текст: `_text` прибирає керівні символи, zero-width і bidi, обрізає довжину. Адрес токенів не більше 10 на пул.
- Безпека: коду ордерів немає, ключів немає. Обсяг змін у межах п. 5, нічого зайвого.

## Знахідки

1. **[блокер, інваріант 1] Одна точка `/chart` з крайнім часовим поясом валить `yield_pool_history` винятком.** `_ts` ловить лише `ValueError` від `fromisoformat`. Але `"0001-01-01T00:00:00+14:00"` (або `"9999-12-31T23:59:59-14:00"`) парситься, дає unix-час поза діапазоном `datetime`, і потім `_day(ts)` → `datetime.fromtimestamp(ts, UTC)` кидає `ValueError: year 0 is out of range`. Відтворено наскрізно через `make_server` з `tests/test_yields.py` і тіло з однією нормальною та однією такою точкою: SDK повертає `UnexpectedToolError` «Error executing tool yield_pool_history» замість конверта. Глобальної сітки в `server.py` немає, тож це відповідь клієнту без конверта, через одне погане значення в чужому API. **Виправлення:** у `_ts` обчислювати й дату в межах `try`, ловити `(ValueError, OverflowError, OSError)` і повертати `None` (точку пропускати як некоректну). Інший варіант: відкидати `ts` поза розумним діапазоном (наприклад, 2015 рік ≤ рік ≤ `now` + 1 день). Додати параметризований тест у `test_pool_history_without_usable_points` (або поруч) з обома рядками, і щоб інструмент лишався `ok=true` на решті точок.

2. **[середня, варто виправити в цьому ж раунді] Кожен виклик `yield_pools` блокує цикл подій на ~0.4–0.65 с.** `filter_pools` на кожен виклик проганяє `pool_row` (кілька `re.sub`, округлення) для всіх ~15 тис. рядків з кешу, а вже потім фільтрує. Мій замір на 15 233 синтетичних рядках форми фікстури: 0.43 / 0.65 / 0.43 с на виклик без tracemalloc. Сервер спільний з колектором (той самий runtime і lifespan), тож це затримка для всіх задач. **Виправлення:** нормалізувати один раз. Або кешувати вже готові `pool_row` (у провайдері перетворення лишається чистою функцією з `analytics`, як уже зроблено з `valid_pool_id`), або спершу фільтрувати дешево за сирими `chain` / `project` / `tvlUsd` / `stablecoin`, а `pool_row` викликати лише для тих, що пройшли. Тест на кількість (`pools_total` / `pools_skipped`) має лишитися правдивим.

3. **[низька] Опис пам'яті перебільшує ефект урізання.** У `docs/changes/`, HANDOFF і картці сказано, що урізання потрібне, «щоб сире тіло не жило в пам'яті». Сирі байти справді не зберігаються. Але мій замір (tracemalloc, 15 233 рядки): розпарсене повне тіло ≈ 27.5 MB, урізаний кеш ≈ 26.5 MB, пік під час завантаження ≈ 40 MB плюс ~10 MB байтів відповіді. Словник з 23 ключами майже такий самий, як з 31, а рядки спільні. У живих рядках `predictions` — окремий словник, тож там економія трохи більша, але порядок той самий. **Виправлення:** у docstring провайдера і в `docs/changes/` записати реальний порядок (~25–30 MB на 10 хв). Якщо мета саме пам'ять, зберігати компактні кортежі або вже нормалізовані рядки (див. п. 2).

4. **[низька] Вікно `days` дає `days + 1` дат, коли найновіша точка внутрішньоденна.** Живцем `days=3` повернув 4 точки (11-16 … 11-19). Тест з `days=7` проходить лише тому, що в фікстурі остання точка вчорашня. Також `apy_mean_7d/30d` рахуються від останньої точки, а `series`, `apy_min` і `apy_max` від `now`. У docstring `analytics` це сказано, в описі інструмента ні. **Виправлення:** або різати за датою (`date > (now − days)`), або дописати в опис «≈ days, the newest point may be today's intraday one; means anchor at the latest point». Тест з внутрішньоденною останньою точкою.

5. **[низька] Розбіжність у походженні форми.** Docstring провайдера: «Shapes as documented (api-docs.defillama.com …)», а `_note` фікстури: «the docs were NOT fetched». Правдиве формулювання: «field names checked live 2026-11-19; documentation not fetched». **Виправлення:** одне речення в docstring `providers/defillama.py`.

6. **[низька, необов'язково] Буквальні bidi- і zero-width-символи в регулярному виразі `_CTRL`** (`analytics/yields.py:26`, скопійовано з `analytics/pendle.py:22`). Працює, але в рев'ю диффу їх не видно (клас «Trojan Source»). Можна записати так: `r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤﻿]"` (`re` розуміє `\uXXXX` і в raw-рядку). Не блокує, бо в репозиторії такий шаблон уже є.

## Що потрібно для approve

Виправити п. 1 і додати на нього тест. П. 2 дуже бажано в цьому ж раунді, п. 3–5 — правки документації чи опису на кілька рядків. Після цього: `make lint` і ті самі тести.

---

# Повторне рев'ю (після раунду виправлень), 19.11

## Вердикт: **approve**

Блокер закрито, medium закрито, з low-пунктів лишається дрібниця (див. нижче), що не блокує.

## Що перевірив сам

- `make lint`: чисто. `uv run pytest -q tests/test_yields.py`: 22 passed.
- **П. 1 (блокер) закрито.** Відтворив наскрізно через `make_server` + `call(..., "yield_pool_history")` з тілом «нормальна точка + точка `0001-01-01T00:00:00+14:00`» і окремо з `9999-12-31T23:59:59-14:00`: в обох випадках `ok=True`, `points_total=1`, `missing=[]`, винятку немає. `_ts` тепер рахує `_day(ts)` у межах `try` і ловить `(ValueError, OverflowError, OSError)`. Є параметризований тест на обидва рядки (`test_yield_pool_history_skips_a_point_outside_the_utc_date_range`).
- **П. 2 (medium) закрито.** `normalise_pools` викликається один раз у `_fetch_pools`, у кеш провайдера кладеться вже нормалізований результат; `filter_pools` лише фільтрує готові рядки. Тест `test_pool_list_is_normalised_once_and_results_match_an_uncached_server` рахує виклики нормалізації (`[7]` на шість запитів) і порівнює відповіді з сервером без кешу: збігаються. Тест `..._cached_slim_in_the_provider` підтверджує один `/pools` на два виклики та `cached=true` на другому.
- **П. 3** закрито: docstring провайдера тепер називає ~26 MB у кеші, ~50 MB пік, ~11 MB байтів відповіді.
- **П. 4** закрито: опис інструмента прямо каже про `days + 1` дат і про прив'язку середніх до останньої точки; є тест з внутрішньоденною останньою точкою.
- **П. 5** закрито: docstring провайдера: «Field names checked live on 2026-11-19; the documentation was not fetched, and the test sample is synthetic».
- `filter_pools` не мутує список кешу на верхньому рівні: повертає `dict(r)`-копії, є тест.

## Нові/лишкові зауваження

1. **[низька, не блокує] Копії поверхневі: `reward_tokens` і `underlying_tokens` у відповіді — ті самі списки, що в кеші.** Перевірив: `o = filter_pools(...)["pools"]; o[0]["reward_tokens"].append("X")` змінює і `normalise_pools(...)["pools"][0]["reward_tokens"]`. Зараз ніхто в шляху інструмента цього не робить (`Builder.build` лише серіалізує), тому живої помилки нема, а docstring провайдера попереджає «callers must not mutate». Але тест перевіряє лише скаляри. Виправлення (за бажанням): копіювати списки токенів у `filter_pools` (`{**r, "reward_tokens": list(...), ...}`) і додати в тест `out[0]["reward_tokens"].append(...)`.
2. **[низька, косметика] П. 6 не виконано:** у `_CTRL` (`analytics/yields.py:27`) лишилися буквальні невидимі символи, а коментар над ним стверджує «escaped so a diff shows them», що неправда. Або замінити на `​-‏‪-‮⁠-⁤﻿`, або прибрати слова «escaped so …» з коментаря. Функціонально працює.
