# Рев'ю (режим C): злиття `market_ticker` → `market_tickers` і `carry_paper_status` → `paper_carry_status(strategy="manual")`

Дата: 2026-11-17. Предмет: незакомічений робочий диф (`git diff`) і новий файл `docs/changes/2026-11-17-mcp-merge-ticker-carry-status.md`; картка `IDEA-20261114-mcp-cleanup`, пункт 2.

## Вердикт: `request changes` (один дрібний блокер, решта в порядку)

Код злиття коректний, поведінку старих інструментів збережено (крім задокументованої зміни форми відповіді для одного символу), тести зелені. Блокує лише один пропущений живий посилальник на стару назву в базі знань: виправлення в один рядок.

## Що перевірив сам

- `make lint`: ruff check і format чисті.
- Тести, що покривають зміну: `tests/test_server.py tests/test_mcp_call.py tests/test_carry_paper.py tests/test_carry_paper_live.py tests/test_tools_no_store.py` плюс змінені в дифі `test_tools_never_raise`, `test_json_cells_guard`, `test_provider_lifecycle`, `test_tools_malformed_200`, `test_tool_failure_extra_8/22/23/24` і доказові для документації `test_tool_desc_audit`, `test_docs_tools_check`, `test_tool_docs`: **358 passed**.
- `scripts/docs_tools_check.py`: зареєстровано 102 інструменти, у заголовку README 102, розбіжностей README / PARITY немає (0 у кожній категорії).
- `scripts/tool_desc_audit.py`: 102 інструменти, 0 зауважень (опис `market_tickers` тепер з одиницями: ціни у валюті котирування, `percentage` у відсотках, `timestamp` у мс, `spread_bps` у б.п.).
- Порівняння з `origin/main`: тіло `carry_paper_status` перенесено у `manual_book_status` (`tools/carry_paper.py`) дослівно: `PAPER_WARNING`, `reject_bad_id`, `store_ready`, `mark_trade` під семафором 3, `closed_all` з limit 5000, показ 200 закритих, округлення, формула `ok`, `except STORE_ERRORS`. Однопозиційний шлях `market_tickers` викликає той самий `rt.exchanges.ticker`, з тим самим полем `ticker` у `missing`, тією самою перевіркою не-dict і тією самою формулою `spread_bps`; набір полів рядка той самий, що в старого `market_ticker` (символ береться з `t["symbol"]`, як і раніше).
- Інваріанти: інструменти не кидають (нові шляхи `id` з книгою A/B → `invalid_input`, `manual` при збої сховища → `store / backend_error`, обидва з тестами); мережа лише через `rt.exchanges` (нових викликів I/O немає); `READ_ONLY` на `paper_carry_status` і `market_tickers` збережено; `EXPECTED_TOOLS` оновлено; README, PARITY, `docs/changes/` оновлено.
- Безпека: диф не торкається `execution/`, ключів, ордерного коду; `scripts/mcp_call.py` (`WRITE_TOOLS`) і `.claude/` старих назв не містять (grep, 0 збігів; `mcp__tradedesk__market_ticker` / `…carry_paper_status` у репозиторії теж 0).
- Залишкові посилання на старі назви (grep по всьому клону без історичних тек): у `src/`, `scripts/`, `.claude/` лише коментарі «the former …»; у тестах лише синтетичні фікстури `test_docs_tools_check` / `test_tool_docs` / `test_tool_desc_audit` (рядки-зразки, не виклики інструмента) і перевірка відсутності `carry_paper_status`. Виняток — пункт 1 нижче.

Наживо (числа з біржі в розділі «Перевірка» файлу змін) не перевіряв: це твердження автора.

## Зауваження

1. **Блокер (низька вага, дешево).** `docs/knowledge/forex.md:15`: «Працюють `market_ohlcv`, `market_ticker`, `perps_*` за ccxt-символом». Це не історичний запис, а чинна база знань, з якої асистент бере, яким інструментом дивитися BingX-форекс; після злиття виклик `market_ticker` дасть «unknown tool». Файл змін стверджує, що всіх викликачів переведено, а цей пропущено. Виправлення: замінити на `` `market_tickers` (один символ, напр. `symbols="NCFXEUR2USD/USDT:USDT"`) ``.

2. **Незначне (тест).** Нова перевірка `isinstance(tk, dict)` у batch-шляху `market_tickers` (файл змін: «`data_unavailable` … для обох шляхів») не покрита тестом: `test_ccxt_list_instead_of_dict_goes_to_missing` тепер викликає лише однопозиційний шлях (`symbols=["BTC/USDT:USDT"]` → `fetch_ticker`). Додати до параметризації другий випадок, напр. `("market_tickers", {"exchange": "binanceusdm", "symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"]})` (або без `symbols`), щоб тест падав, якщо перевірку прибрати. Не блокує: до цієї зміни batch-шлях такої перевірки не мав зовсім.

3. **Інформаційно (поведінка, задокументовано).** `market_tickers(symbols=["X"])` раніше йшов через `fetch_tickers`, тепер через `fetch_ticker`. Форма відповіді та сама; відмінність лише в тому, який ендпоінт біржі викликається (і в іншому кеші). Внутрішніх викликачів з одним символом не знайшов (`collector/assistant_snapshot.py` передає три). Дій не треба.

4. **Інформаційно, поза обсягом.** `CLAUDE.md` («Інструменти MCP tradedesk (109 шт.…)») відстав ще до цієї зміни (було 104, стало 102). Не правити в цій гілці; варто оновити при наступній правці CLAUDE.md власником або координатором.

## Що зробити до злиття

- Пункт 1 (обов'язково), пункт 2 (бажано, у тому самому прогоні). Після правки достатньо `make lint` і `uv run pytest -q tests/test_tools_malformed_200.py`.
