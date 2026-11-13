# 2026-11-13 · Тести The Graph на живому зразку (пункт 364)

Пункт 364 [постійної картки](../ideas/IDEA-20261102-standing-improvements.md); закриває відкрите з `docs/HANDOFF.md` («The Graph: перевести `tests/test_thegraph.py` на живий зразок»). Змінено лише `tests/test_thegraph.py`; `src/`, `providers/`, `execution/` не чіпав, мережі немає.

- **Що було.** Живий зразок `tests/fixtures/thegraph_live_2026-10-29.json` (Polymarket main subgraph, 6 відповідей шлюзу, записано `scripts/thegraph_sample.py`) лежав з 29.10, але жоден тест його не читав. Happy path, курсор `id_gt` і помилка GraphQL перевірялись лише на вигаданих відповідях (`thegraph_synthetic_2026-10-28.json`: сутність-заглушка `positions`, блок 78000000).
- **Що стало.** Тести відтворюють записані відповіді (статус і тіло) і звіряють, що провайдер шле той самий запит, що дав запис (шлях, текст запиту, змінні):
  - `meta`: блок 96398364, `timestamp`, `hasIndexingErrors=false`; запит = `META_QUERY` дослівно;
  - `polymarket` з запитом і змінними `page_2`: перший `conditions.id` збігається із записом;
  - `query_pages` на `page_1` + `page_2` (`page_size=2`, `max_pages=2`): 4 справжні `id` у порядку, `complete=false` (обидві сторінки повні), курсор другого запиту = `id` останнього рядка першої сторінки, як у записі; запит оголошує `$lastId: ID!`;
  - `error_query`: `backend_error`, і `reason: detail` дорівнює записаному результату ``backend_error: GraphQL error: Type `Query` has no field `tradedeskNoSuchField` ``; у параметризованому тесті «помилка не кешується» варіант `error_query` теж бере живе тіло;
  - окремий тест: зразок — це Polymarket main subgraph, усі відповіді HTTP 200.
- **Синтетика лишилась** для форм, яких у живому зразку немає: часткова помилка (`errors` + `data`), auth-помилка в HTTP 200, коротка остання сторінка (`complete=true`), а також для тестів ключа, бюджету й скрипта зразка.
- **Що стане кращим.** Зміна `META_QUERY`, курсора або розбору `errors`, несумісна з тим, що реально відповідає шлюз, тепер падає в тесті без мережі.
- **Перевірка.** `uv run pytest -q tests/test_thegraph.py`: 35 passed за 0.8 с. `make lint` чисто. Повний набір запускає координатор.
- **Відкрите (без змін).** id підграфа P&L (`TRADEDESK_GRAPH_PNL_SUBGRAPH_ID`) досі невідомий, тож `polymarket_pnl` перевіряється лише на синтетиці.
