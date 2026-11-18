# 2026-11-18 — три `coinalyze_*` злито в `perps_*` з параметром `source` (картка mcp-cleanup, пункт 2, четверте злиття)

Рішення власника Q-20261114-1 (А1), картка [`IDEA-20261114-mcp-cleanup`](../ideas/IDEA-20261114-mcp-cleanup.md). Три інструменти історії Coinalyze прибрано з MCP; ту саму відповідь тепер дають відповідні `perps_*` з `source="coinalyze"`. Інструментів було 102, стало 99.

## Стара → нова

| Було | Стало |
|---|---|
| `coinalyze_oi_history(symbol, interval?, days?, margin?, in_usd?, exchanges?)` | `perps_oi_history(source="coinalyze", base=<symbol>, interval?, days?, margin?, in_usd?, exchanges?)` |
| `coinalyze_funding_history(symbol, interval?, days?, margin?)` | `perps_funding_history(source="coinalyze", base=<symbol>, interval?, days_back=<days>, margin?)` |
| `coinalyze_long_short_history(symbol, interval?, days?, margin?)` | `perps_long_short_ratio(source="coinalyze", base=<symbol>, interval?, days?, margin?)` |
| `perps_oi_history` / `perps_funding_history` / `perps_long_short_ratio` без `source` | без змін (`source="exchanges"` за замовчуванням: Binance / Bybit / OKX / Hyperliquid, як і раніше) |

- `symbol` → `base` (той самий тікер: `BTC`, `eth` — регістр не важливий, як і раніше).
- Типові значення Coinalyze ті самі: `interval="daily"`, `days=30` (для funding — `days_back=30`), `margin="usdt"`, `in_usd=true`, `exchanges` — лише Binance.
- **Funding:** вікно — спільний параметр `days_back` (окремого `days` немає, щоб у одному інструменті не було двох вікон). Для `source="coinalyze"` — цілі дні від 1 до 365 (дробове або < 1 — `ok=false`, `missing: days_back / invalid_argument`, запиту немає); межу `days_back` у схемі піднято з 180 до 365, а для `source="exchanges"` понад 180 тепер дає той самий `invalid_argument` у конверті (раніше — помилка валідації схеми MCP).
- **OI і лонг/шорт:** `interval` / `days` / `margin` (і для OI `in_usd`, `exchanges`) діють лише з `source="coinalyze"`; `period` / `limit` / `include_series` — лише з `source="exchanges"`, у Coinalyze ігноруються (ряди Coinalyze повертаються завжди, як у старих інструментах).
- Параметр, що не стосується обраного джерела, ігнорується; явно передані `exchanges` — з попередженням: у `perps_oi_history` без `source="coinalyze"` («exchanges applies to source='coinalyze' only; ignored here», відповідь та сама, Binance і Bybit поруч) і в `perps_funding_history` з `source="coinalyze"` (там `exchanges` — це список ccxt-бірж, «applies to source='exchanges' only»).

## Відповідь і числа

- З `source="coinalyze"` відповідь — та сама, що в старого інструмента: `data = {rows, stats, symbol, interval, unit, days}` (для OI ще `exchanges`), ті самі `evidence` (провайдер `coinalyze`), `missing` (поля `open_interest[.<біржа>]`, `funding`, `long_short_ratio`, `symbol`, `exchanges`) і попередження (Binance only, USD = монети × close інтервалу, hourly funding не звірено тощо). Форма відповіді `source="exchanges"` (`venues`, `cross_venue`) не змінилась.
- Тіла старих інструментів перенесено без змін у функції `oi_history`, `funding_history`, `long_short_history` у `tools/coinalyze.py` (їм лише передається `Builder` від `perps_*`); `perps_*` викликають їх на початку при `source="coinalyze"`. `liquidations_history` лишився окремим інструментом у тому самому модулі.
- Перевірено побайтно на фікстурах тестів: старий модуль з `origin/main` і новий маршрут на одному `Runtime` — 19 викликів (OI daily / hourly з обрізанням до 30 днів / coin-margined, funding daily / hourly / 365 днів, лонг/шорт, поганий символ, 503, без ключа, сума Binance + Bybit, одна біржа впала, невідома біржа) дали однакові конверти (без `as_of` і часу запиту в `evidence`).
- Описи інструментів англійською з одиницями й застереженнями старих інструментів (funding — десятковий дріб за інтервал виплати, не річний, середнє закриттів — не основа для carry; лонг/шорт — частки 0–1, рахунки, не ноціонал, мітка часу — знімок у t; OI — USD Coinalyze = монети × close інтервалу, у день обвалу зсунуто).

## Перевірка

- Тести: `test_coinalyze_history` і `test_coinalyze_exchanges` (еталони старих інструментів перенесено на `source="coinalyze"`, ті самі числа; нові: дробові `days_back` → `invalid_argument` без запиту, `days_back=365` доходить до Coinalyze, `exchanges` з Coinalyze-funding — попередження, у `evidence` лише `coinalyze`), `test_server` (`EXPECTED_TOOLS` 102 → 99; новий `test_perps_source_exchanges_argument_rules`: `exchanges` у `perps_oi_history` без Coinalyze — попередження, `days_back=200` — `invalid_argument`), `test_tools_provider_outage` (новий: Coinalyze-маршрут трьох інструментів при 500 і тайм-ауті — конверт, `coinalyze` у `missing`), `test_tools_envelope_shape` (новий: форма конверта Coinalyze-маршруту; поріг «ok=true» 100 → 95, бо інструментів 99), `test_tools_never_raise`, `test_docs_tool_coverage` (поріг «список не обрізано» 100 → 90).
- Наживо 18.11 з сервера через `build_server()` (ключа Coinalyze в агентському оточенні немає): `perps_oi_history` і `perps_long_short_ratio` BTC 1h — `ok=true`, Binance і Bybit, `missing` порожній; `perps_funding_history` BTC 7 д — `ok=true`, hyperliquid / binanceusdm / bybit / okx; `days_back=200` — `ok=false`, `days_back / invalid_argument`; усі три з `source="coinalyze"` — `ok=false`, `missing: coinalyze / config` (ключа немає), без винятку. Стара `coinalyze_funding_history(BTC, 7 днів)` у працюючій службі того ж дня: 8 денних рядків, latest 3.132e-05 за інтервал, `symbol=BTCUSDT_PERP.A` — після злиття й перезапуску `perps_funding_history(source="coinalyze", days_back=7)` має дати ті самі числа.

## Хто викликав старі назви

- Пошук назв (`rg`) по `scripts/`, `src/` (з промптами), `deploy/`, `.claude/`, панелі (`scripts/dashboard.py`): викликів немає. `scripts/mcp_call.py` (`WRITE_TOOLS`) і `scripts/nightly_agents.sh` цих назв не містять — правок не треба.
- Переведено: тести вище, `README.md` (рядки перпів і «Історія Coinalyze», кількість 102 → 99), `docs/PARITY.md` (три рядки coinalyze і кількість інструментів), `docs/BACKLOG.md`, `docs/HANDOFF.md`.
- Старі назви лишились в історичних записах (`CHANGELOG.md`, `docs/changes/`, `docs/reviews/`, `docs/agents/runs/`, `docs/history/`, `docs/library/LIB-20261113-x-trader_xo.md`) — їх не переписуємо.
- `CLAUDE.md` досі каже «104 шт.» — застаріло, правка за власником.
- Служба MCP підхопить зміну після злиття гілки й перезапуску; до того чат і агенти бачать старі назви.
