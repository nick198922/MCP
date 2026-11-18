# 2026-11-18 — прибрано шість інструментів MCP (картка mcp-cleanup, пункт 3, «Б2»): 97 → 91

Рішення власника Q-20261114-1 (Б2), картка [`IDEA-20261114-mcp-cleanup`](../ideas/IDEA-20261114-mcp-cleanup.md), підстава — [таблиця інструментів 14.11](../reviews/2026-11-14-mcp-inventory.md) (0 викликів за 30 днів у кожного).

| Було | Стало |
|---|---|
| `carry_paper_open`, `carry_paper_close` | прибрано: ручний папір перп-перп замінив рушій колектора (книга B, `paper_carry_status`). Пари, відкриті раніше, і далі показує `paper_carry_status(strategy="manual")`; у живій БД 18.11 ручних пар не було (ні відкритих, ні закритих) |
| `fib_retracement` | прибрано (ТА без механізму, рішення 17.10); формула лишилась в `analytics/retracement.py`, промпт `fomc_playbook` рахує драбину сам: `L + (P0 − L) × level` |
| `strategy_backtest_momentum` | прибрано (бектести живуть у `scripts/verify_*`); чистий бектест лишився в `analytics/backtest.py` з тестами |
| `events_seed_load` | разова дія: `uv run python scripts/events_seed_load.py [--db PATH]` (логіка — функція `events_seed_load(rt)` у `tools/events.py`, без змін) |
| `rules_presets_load` | разова дія: `uv run python scripts/rules_presets_load.py [--only …] [--enable-disabled] [--db PATH]` (функція `rules_presets_load(rt, …)` у `tools/rules.py`, без змін; пороги пресетів не бектестовані — скрипт про це пише) |

- `positions_*` лишились.
- `WRITE_TOOLS` у `scripts/mcp_call.py`: 14 → 10 назв. `scripts/nightly_agents.sh` агентам правити не можна, тому його заборони (рядки 464–465) ще містять чотири прибрані назви; `tests/test_mcp_call.py` допускає саме ці чотири зайві (`REMOVED_WRITE_TOOLS`) і перевіряє, що на сервері їх немає. Власнику: прибрати з рядків 464–465 `"mcp__tradedesk__events_seed_load" "mcp__tradedesk__rules_presets_load"` і `"mcp__tradedesk__carry_paper_open" "mcp__tradedesk__carry_paper_close"`.
- `src/tradedesk/tools/backtest.py` більше не реєструється (`tools/__init__.py`), але файл лишився: агентам видаляти файли з git не можна. Власнику — видалити.
- Обидва скрипти без `--db` пишуть у базу служби: `$TRADEDESK_DB_PATH`, якщо задано, інакше `/var/lib/tradedesk-mcp/tradedesk.sqlite` (як `scripts/binance_import.py`; у shell змінної немає, служба бере її з `/etc/tradedesk-mcp.env`). Шлях запису друкується в stderr (`store: …`).
- Тести: `EXPECTED_TOOLS` 97 → 91; тести інструментів-завантажувачів переписано на функції (`test_events`, `test_rules`, `test_tool_failure_extra_5` / `_6`) + нові тести скриптів; тести статусу ручної книги тепер кладуть пару прямо в сховище (`test_carry_paper`, `_23`, `_24`); тести відкриття / закриття пар, fib і бектест-інструмента прибрано (аналітичні тести лишились), закриті ручні пари перевіряє новий тест через `Store` (`summary_closed`, `entry_tradable`, `closed`); пороги лічильників у `test_tools_envelope_shape`, `test_tools_provider_outage`, `test_tools_bad_input` тепер рахуються від `len(EXPECTED_TOOLS)` із запасом, `test_tools_no_store` знижено.
- Наживо 18.11 з сервера: `build_server()` реєструє 91 інструмент, прибраних серед них немає; `paper_carry_status(strategy="manual")` — `ok=true`, порожньо; обидва скрипти на тимчасовій БД: сід подій — 129 вставлено, пресети — 8 вставлено (що повтор нічого не додає, перевіряють тести). У живу БД служби скрипти не запускались (там 0 подій і 0 правил — запуск за власником).
- Служба MCP побачить зміну після злиття й перезапуску.
