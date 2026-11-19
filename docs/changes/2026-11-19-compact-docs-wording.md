# Точніші описи стислих відповідей (пункт 412)

- `README.md`: у рядках `system_health`, `market_tickers`, `cycle_regime`, `polymarket_search` — по одному реченню про стислу типову відповідь (що саме лишається) і параметр `include_details=true`.
- `system_health`: в описі замість «loses nothing» точно — нічого не втрачено, крім імен інструментів (замість `tools` — `tool_count`). README і рядок у `docs/PARITY.md` виправлено так само.
- `polymarket_search`: в описі явно — `count` = скільки ринків знайдено (до `limit`), а не довжина `markets`; `markets` містить до 10 перших (`shown`).
- Тести на ключові фрази й на поведінку (`tool_count` замість `tools`; `count` = 20, `shown` = `len(markets)` = 10) — `tests/test_tool_descriptions_units.py`. Логіку не змінено.
