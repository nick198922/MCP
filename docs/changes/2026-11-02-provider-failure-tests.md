# 2026-11-02 — тести «збій провайдера → конверт» (інваріант 5)

Пункт 21 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md).

**Аудит.** Звірив 108 інструментів з `EXPECTED_TOOLS` (`tests/test_server.py`) з тестами: розбір тестів через `ast` (які тести викликають інструмент і чи ламають провайдера: `deribit_status`, 4xx/5xx у `MockTransport`, винятки ccxt, `okx` / `DOWN/` у `FakeEx`) плюс ручна перевірка спірних випадків (параметризовані тести, як у `test_coinalyze_history.py`, і `test_hl_coin_case.py`, рахуються як покриття). Інструменти без провайдера (`strategy_*`, `odds_devig`, `option_calc`, `fib_retracement`, журнал / правила / події на БД) прогалинами не вважав.

**Закрито 8** (`tests/test_provider_failures.py`, 9 тестів): `deribit_futures_curve`, `deribit_dvol`, `deribit_largest_trades` (Deribit 503), `hyperliquid_overview` (HL 503), `global_market`, `top_coins` (CoinGecko 503), `polymarket_market` (gamma 503 → `ok=false`; clob 503 → картка є, без книг, 2 записи в `missing`), `polymarket_history` (clob 503). Порушень інваріанта 1 не знайдено, код інструментів не змінювався.

**Лишилось (13):**
- Deribit, той самий шлях книги опціонів, що й покритий `deribit_vol_surface`: `deribit_option_chain`, `prob_from_options`, `deribit_expected_move`, `deribit_oi_by_strike`, `deribit_atm_term`, `deribit_skew_term` — достатньо `state.deribit_status = 503`.
- ccxt: `market_ohlcv` (символ `DOWN/…` у `FakeEx`), `market_orderbook`, `market_markets` (потрібен `FakeEx` з винятком у `fetch_order_book` / `load_markets`).
- Кілька бірж (частковий збій, одна біржа в `missing`, решта є): `perps_open_interest`, `perps_oi_history`, `perps_long_short_ratio`, `perps_basis_compare`.
