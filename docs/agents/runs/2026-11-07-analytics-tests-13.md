**Крок:** p, постійна картка `standing-improvements`: розблокованих пунктів не було, додано пункт 209 (тести `analytics/`, BACKLOG 6.9).

**Результат:**
• `tests/test_analytics_direct_13.py`: 15 тестів на 12 функцій (5 копій Wilder ADX/DMI, `sma_valid`, `ema_valid`, `range_setups`, `perp_symbols`, `bars_from_rows`, `ny_days`, `oriented_signal`). Без тесту лишилось 2 із 2173.
• `src/` не змінено. `make lint` чисто, `make test`: 6915 passed, 3 skipped.
• Знайдено, не виправлено: `perp_symbols` пропускає однолітерні бази (`WUSDT`, `TUSDT`); копії ADX розходяться на пласких сегментах від 2n−1 барів.

**Критик:** approve ([рев'ю](../../reviews/2026-11-07-analytics-tests-13.md)), блокерів немає.

**Далі:** `owner`. Питання власнику: чи виправляти `perp_symbols` для однолітерних тикерів? Це змінить склад подій у `verify_news_events.py` і `verify_perp_launch_existing.py`. Записано в BACKLOG 6.9.
