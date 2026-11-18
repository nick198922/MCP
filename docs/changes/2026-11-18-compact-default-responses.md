# 2026-11-18 · Стислі відповіді за замовчуванням: `market_flow`, `hyperliquid_funding_history`

Пункт 379 постійної картки, BACKLOG «З тижневого аудиту 2026-11-14» → «великі відповіді за замовчуванням» (перші два інструменти зі списку).

- `market_flow`: параметр `include_series` тепер типово `false` (було `true`). Без нього — зведення за 4 год / 24 год / вікно, квадранти, речення українською і останні значення по біржах; повний ряд по барах (`data.series`) — `include_series=true`. Одиниці й числа зведень не змінились.
- `hyperliquid_funding_history`: новий параметр `include_series` (типово `false`). Без нього — лише `stats`; сирі погодинні виплати `data.entries` [{ts_ms, rate}] — `include_series=true` (як у `perps_funding_history`).
- Розмір типової відповіді (JSON-конверт, символи), живий виклик BTC 18.11: `market_flow` 68 159 → 6 946 (з `include_series=true` — як і раніше, 68 159); `hyperliquid_funding_history` 32 926 → 902 (повний — 32 926, 720 виплат за 30 днів). На фейках тестів: 64 610 → 7 437 і 9 560 → 884.
- Споживачі: колектор знімка асистента (`collector/assistant_snapshot.py`) уже передає `include_series` явно (бриф — `false`, вкладка «Потоки» — `true`), панель і бриф не змінюються; скрипти поле `entries` не читають.
- Тести: `tests/test_compact_defaults.py` — типова відповідь обох інструментів ≤ 8 000 символів JSON, повний ряд за параметром; у `tests/test_market_flow.py` і `tests/test_review_leftovers_nonfinite.py` виклики, що читають ряд, передають `include_series=true` або перевіряють лише `stats`.
