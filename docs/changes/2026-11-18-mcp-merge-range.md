# 2026-11-18 — три `range_*` злито в `range_detect` з параметром `view` (картка mcp-cleanup, пункт 2, останнє злиття)

Рішення власника Q-20261114-1 (А1), картка [`IDEA-20261114-mcp-cleanup`](../ideas/IDEA-20261114-mcp-cleanup.md). `range_setup` і `range_setup_scan` прибрано з MCP; ту саму відповідь тепер дає `range_detect` з `view="setup"` / `view="scan"`. Інструментів було 99, стало 97. Пункт 2 картки закрито.

## Стара → нова

| Було | Стало |
|---|---|
| `range_detect(venue, symbol, …)` | без змін (`view="ranges"` за замовчуванням) |
| `range_setup(venue, symbol, …)` | `range_detect(view="setup", venue, symbol, …)` |
| `range_setup_scan(venue, symbols, …)` | `range_detect(view="scan", venue, symbols, …)` |

- Назву `range_detect` лишено, бо її вже знають чат, `CLAUDE.md` і `docs/VERIFICATION_PROTOCOL.md`; так само попередні злиття лишали назву інструмента, що виживав (`deribit_skew_term`, `perps_*`).
- **Типові значення — як у старого інструмента кожного view** (параметри з `None` у схемі добираються за `view`): `timeframe` 1h / 4h / 4h, `lookback_bars` 500 / 300 / 300, `max_width_pct` 10 / 15 / 15, `source` `store` / `live` / лише live. Решта (`min_bars` 20, `swing_lookback` 3, `sweep_wick_ratio` 0.5, `stop_mult` 2, `stop_min_offset_pct` 0.1, `min_rr` 2, фільтри скану) і межі параметрів ті самі.
- `symbol` у схемі тепер необов'язковий: потрібен для `ranges` і `setup`, без нього — `ok=false`, `missing: symbol / invalid_input`, запиту немає. `scan` бере `symbols` (до 50, як і раніше); без `symbols` — `missing: symbols / invalid_input`; порожній список — як раніше (`symbols / config`).
- Параметр іншого view ігнорується (як у злитті coinalyze); з попередженням — два випадки, де легко помилитися: `symbols` поза `scan` («symbols applies to view='scan' only; ignored here») і `source="store"` у `scan` («view='scan' reads live ccxt bars only; source='store' ignored», скан, як і раніше, читає живі бари).

## Відповідь і числа

- Тіла старих інструментів перенесено без змін у вкладені функції `_range_detect_view`, `_range_setup_view`, `_range_scan_view` у `tools/history.py` (їм лише передається `Builder`); `range_detect` вибирає їх за `view`. Ключі `data`, `missing`, `evidence` і попередження (`Unverified edge…` для setup / scan) ті самі; поля `view` у відповіді немає.
- Перевірено побайтно на фейках тестів: старий код (до правки) і новий маршрут на одному `Runtime` (FakeEx + сховище з 1h-барами й 4h-барами зі sweep) — 19 викликів (ranges: live з тестовими параметрами й типовими, store типові, store 4h, порожнє сховище, біржа впала, інші swing / wick / deviation; setup: store типові, `use_tight_stop` з `min_rr=1.5`, store 1h без сетапу, live типові й тестові, біржа впала; scan: тестовий, типові з біржею, що впала, `max_sweep_age_bars=0`, `exclude_ambiguous` + `max_sweep_age_bars=1000` + `min_rr=1`, порожній список, усі впали) дали однакові конверти (без `as_of` і часу запиту в `evidence`).
- Опис інструмента англійською зібрано з трьох старих: одиниці (ціни в валюті котирування, `*_pct` і `max_width_pct` у відсотках, мітки часу — unix ms, rr — відношення), типові значення кожного view, причини відмов скану і застереження: сетап — механічна пропозиція, не порада; walk-forward значущої переваги не знайшов (те саме попередження `SFP_EDGE_WARNING` у `warnings`).

## Перевірка

- Тести: `test_ranges` (еталони старих `range_setup` / `range_setup_scan` перенесено на `view`; нові: `test_range_detect_setup_view_reference_numbers` — числа `range_setup(source="store")` на 4h-барах зі sweep, записані до злиття; `test_range_detect_views_keep_the_old_defaults` — timeframe / limit запиту й `max_width_pct` кожного view; `test_range_detect_default_view_reads_the_store`; `test_range_detect_view_argument_rules`), `test_tools_provider_outage` (новий: live `ranges`, `setup` і `scan` при 500 і тайм-ауті — конверт, `ccxt:binanceusdm` у `missing`; загальний обхід кличе `range_detect` лише з `venue`), `test_tools_envelope_shape` (новий: форма конверта `setup` зі сховища і `scan`), `test_server` (`EXPECTED_TOOLS` 99 → 97), `test_tools_never_raise`, `test_tools_no_store`, `test_tools_corrupt_store`, `test_tool_failure_extra_13` / `_14`, `test_review_leftovers_nonfinite` (ключі `range_setup*` → `range_detect` з `view`).
- Наживо 18.11 з сервера через `build_server()` (binanceusdm): `ranges` live BTC — `ok=true`, 500 барів 1h; `setup` BTC — `ok=true`, 4h, long, entry 73437.7, stop 72049.0, tp1 74602.95, rr1 0.839, `tradable=false`; той самий виклик старого `range_setup` у працюючій службі того ж дня дав ті самі entry / stop / tp / rr; `scan` BTC / ETH / SOL / XRP — `ok=true`, 2 сетапи, 2 `no_sweep_in_range`; скан з неіснуючим символом — `fetch_failed` у `rejected`, `ccxt:binanceusdm` у `missing`; `setup` без `symbol` — `ok=false`, `symbol / invalid_input`, без винятку.

## Хто викликав старі назви

- Пошук назв (`rg`) по `scripts/` (з панеллю `scripts/dashboard.py`), `src/` (з промптами й колектором), `deploy/`, `.claude/`: викликів `range_setup` / `range_setup_scan` немає. `scripts/mcp_call.py` (`WRITE_TOOLS`) і `scripts/nightly_agents.sh` цих назв не містять — правок не треба.
- Переведено: тести вище, `README.md` (рядок «Історія», кількість 99 → 97), `docs/PARITY.md` (два рядки й кількість), `docs/BACKLOG.md`, `docs/HANDOFF.md`, коментарі в `tools/history.py` і `analytics/sfp_backtest.py`.
- Старі назви лишились в історичних записах (`CHANGELOG.md`, `docs/changes/`, `docs/reviews/`, `docs/agents/runs/`, `docs/history/`, `docs/verification/`, `docs/library/`, `docs/ideas/`) — їх не переписуємо.
- `CLAUDE.md` досі каже «104 шт.» — застаріло, правка за власником.
- Служба MCP підхопить зміну після злиття гілки й перезапуску; до того чат і агенти бачать старі назви.
