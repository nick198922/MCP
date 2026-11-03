# 2026-11-03 — `source="store"` бере останні бари

Пункт 29 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md), BACKLOG «Ідеї без пріоритету» (закрито).

- **Виправлено:** `range_detect`, `range_setup` і `market_regime` з `source="store"` аналізували **найстаріші** бари з `ohlcv_hist` (`ohlcv_range(since_ts=0, limit=2N)` за зростанням `ts`, потім `rows[-N:]`). Тепер беруть **останні N** барів, у порядку зростання `ts`.
- **Store:** `Store.ohlcv_range(...)` отримав параметр `newest: bool = False`: з `newest=True` запит іде `ORDER BY ts DESC LIMIT N`, результат розвертається у зростання. Типова поведінка (без `newest`) не змінилась; інші виклики (`ohlcv_history`, `event_study`) не зачеплено.
- **Описи:** у `lookback_bars` (`range_detect`, `range_setup`, `range_setup_scan`, `market_regime`) тепер сказано, що в store-режимі це найновіші N рядків `ohlcv_hist`.
- **Тести:** БД з 1000 барів, N=100: store-рівень (`tests/test_ohlcv_hist.py`), `range_detect` і `range_setup` (шпигун на `classify_ranges` бачить рівно бари 900–999, `tests/test_ranges.py`), `market_regime` (900 барів падіння + 100 росту → `up`, `first_ts`/`last_ts` = бари 900 і 999, `tests/test_regime.py`).
- **Live-перевірка не потрібна:** зміна стосується лише читання локальної SQLite; режим `source="live"` не змінено.
