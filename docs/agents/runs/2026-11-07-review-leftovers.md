**Крок:** постійна картка `IDEA-20261102-standing-improvements`, новий пункт 254 (залишки рев'ю критика «не блокує»); інших розблокованих пунктів немає.

**Результат:**
• Виправлено 3 зауваження з рев'ю: NaN/inf у виплатах funding (paper carry, `perps_funding_history`, `hyperliquid_funding_history`, `funding_carry_scan`, `funding_spread_scan`); `OverflowError` у розборі рядків (5 інструментів); живі бари `range_detect`/`range_setup` (`None`-об'єм давав `TypeError`).
• 33 нові тести (21 падали на старому коді), `make lint` чисто, `make test`: 7173 passed, 3 skipped.
• Решту записано в BACKLOG 6.52 (знайдено й поточні знімки funding без фільтра NaN/inf).

**Критик:** approve (C, `docs/reviews/2026-11-07-review-leftovers.md`); дрібне: `_live_bars` приймає inf як ціну/об'єм — рядок у BACKLOG не додано, залишилось у рев'ю.

**Далі:** `owner` (PR). Нових питань немає.
