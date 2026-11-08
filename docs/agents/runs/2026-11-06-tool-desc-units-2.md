**Крок:** p, постійна картка `IDEA-20261102-standing-improvements` (Порядок 1); відкритих пунктів лишалось менше трьох (8a чекає сервера), додано і виконано пункт 185.

**Результат:**
• Одиниці додано в описи `market_ohlcv`, `hyperliquid_candles`, `event_study`, `prob_from_options`, `touch_probability` (лише текст, логіка й схеми без змін).
• `scripts/tool_desc_audit.py`: список `REQUIRED_UNITS` для цих п'яти + тест; аудит реєстру — зауважень 0.
• BACKLOG 6.34 оновлено; `make lint` чистий, `make test`: 6715 passed, 3 skipped.

**Критик:** C (sonnet) — approve; не блокує: регулярки перевіряють лише наявність слова; у `event_study` «bars in the window» неточне ([рев'ю](../../reviews/2026-11-06-tool-desc-units-2.md)).

**Далі:** owner. Нових питань немає (відкрите Q-20261106-1 чекає відповіді).
