**Крок:** p, постійна картка standing-improvements; відкритим лишався лише 8a (чекає сервера), тож додано й виконано пункт 62.

**Результат:**
• BACKLOG: слова «Вимкнене» у рядках ≈ 70, 72, 78 звірено з `deploy/features.env`.
• `option_xvenue`, `tradfi_spread`, `hl_oi_cap` уже ввімкнені: дописано дату й посилання, зміст пунктів не змінено.
• Код не чіпали; `make lint` чистий, `make test`: 4434 passed, 2 skipped.

**Критик:** C (sonnet), approve, [рев'ю](../../reviews/2026-11-04-backlog-disabled-sweep.md); дві дрібниці не блокують (подвійна згадка ввімкнення в `option_xvenue`).

**Далі:** `owner`. Очікує власника ще гілка `agent/2026-11-04-library-seller-exhaustion-card`. Нових питань немає.
