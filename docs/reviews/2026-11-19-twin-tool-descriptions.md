# Рев'ю: описи близнюків-інструментів (2026-11-19)

Вердикт: **approve**. Блокерів немає.

Перевірено сам:
1. `git diff -- src`: змінено лише рядки `description=` у 12 інструментах. Параметрів, назв, логіки немає. Описи англійською.
2. Одиниці й факти звірено з кодом:
   - `events_list`: ORDER BY ts LIMIT дає найстаріші події. Вікно від 00:00 до 23:59 UTC. `ts_ms` ділиться на 1000 і зберігається в секундах.
   - `deribit_dvol`: відсотки, `hours_back` не більше 720, тобто 30 днів. `deribit_vrp` і `iv_rank` у десяткових дробах.
   - `liquidations_summary`: `window_minutes` не більше 1440, поля `ts`, `qty`, `imbalance` на місці.
   - `liquidations_history`: ліміти 365 днів і 30 днів погодинно збігаються з `Days`.
   - `rules_disable`: за замовчуванням `enable=False`, `delete` має пріоритет, `disabled_ts` дорівнює `None` при увімкненні, невідомий id іде в `missing`.
3. `make lint` проходить. `pytest tests/test_tool_desc_audit.py tests/test_server.py tests/test_tool_metadata.py`: 95 passed.

Не перевірено (мінорне, не блокує): рядки про `perps_open_interest` (оцінка oi_base × last, Deribit oi_base null), `ohlcv_history` (обсяг як у ccxt) і `positions_list` (одиниці греків). Їх бажано звірити в наступному проході аудиту описів.
