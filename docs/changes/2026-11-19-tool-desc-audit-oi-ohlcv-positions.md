# 2026-11-19 · Звірка описів perps_open_interest, ohlcv_history, positions_list з кодом

Пункт 403 постійної картки (рев'ю «описи близнюків», «не перевірено, не блокує»). Описи інструментів не змінено: розбіжностей з кодом не знайдено.

- **`perps_open_interest`**: `oi_usd` оцінюється як `openInterestAmount × last` з тікера ccxt лише тоді, коли біржа не дала `openInterestValue`, і ця біржа названа в `warnings`. Для Deribit `oi_base` = null, а `oi_usd` — USD open interest перпетуала. Для Hyperliquid `oi_usd` = монети × mark. На okx `oi_base` у контрактах: ccxt бере `oi`, а не `oiCcy`. Наживо 19.11 BTC: okx 3,27 млн контрактів × 0,01 ≈ 32,7 тис. BTC, що збігається з `oi_usd` / ціна; binance і bybit оцінено, обидві названі в `warnings`.
- **`ohlcv_history`**: колектор зберігає `bar[5]` ccxt як є. Для перпів binanceusdm, bybit і okx `parse_ohlcv` бере стовпець обсягу в базовому активі (okx — індекс 6, `volCcy`). У USD не перераховується. `ts` — Unix мс; межі since/until включні; `limit` рахує від найстаріших барів.
- **`positions_list`**: греки віддаються так, як їх ввели через `positions_add`, без перерахунку. Це ті самі одиниці, на які розраховує `positions_risk`: delta в монетах, vega в USD на 1 пункт вола, theta в USD за календарний день.
- Тест `test_unit_lines_checked_against_the_code` у `tests/test_tool_desc_audit.py` закріплює ці фрази в реальних описах. Він також перевіряє, що в описі OI названо всі біржі з `PERP_EXCHANGES` і що одиниці греків у `positions_list` збігаються з `positions_add`.
- `make lint` чисто; `tests/test_tool_desc_audit.py`, `tests/test_server.py`, `tests/test_tool_metadata.py` — 96 passed.
