# 2026-11-03 — тести «збій провайдера → конверт», решта 13 (інваріант 5)

Пункт 23 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md); продовження [2026-11-02](2026-11-02-provider-failure-tests.md). Пункт 6.5 у `docs/BACKLOG.md` закрито.

- **Deribit, книга опціонів (Deribit 503 → `ok=false`, `deribit` у `missing`):** `deribit_option_chain`, `prob_from_options`, `deribit_expected_move`, `deribit_oi_by_strike`, `deribit_atm_term`, `deribit_skew_term`.
- **ccxt, одна біржа:** `market_ohlcv` (символ `DOWN/…`, `NetworkError`), `market_orderbook` (`NetworkError` у `fetch_order_book`), `market_markets` (`ExchangeError` у `load_markets`); для двох останніх у тесті є `_BrokenEx` (підклас `FakeEx`), `conftest.py` не змінювався.
- **Кілька бірж:** `perps_long_short_ratio`, `perps_oi_history` (Bybit 503 → лишається Binance, `bybit` у `missing`; обидві впали → `ok=false`); `perps_open_interest`, `perps_basis_compare` (HL і Deribit 503, okx без даних → лишаються Binance і Bybit, усі три в `missing`; лише okx + HL + Deribit → `ok=false`).
- Разом 17 нових тестів у `tests/test_provider_failures.py`. Порушень інваріанта 1 не знайдено, код інструментів не змінювався.
