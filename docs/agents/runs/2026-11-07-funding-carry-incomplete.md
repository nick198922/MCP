**Крок:** p — постійна картка `standing-improvements` (всі картки з `Порядок: 0` заблоковані часом, воротами потужності чи рішенням власника), пункт 251.

**Результат:**
• `funding_carry_scan`: позначка `incomplete_history` у ряду й `summary.incomplete_history`; такий символ не потрапляє в `above_entry_with_spot`.
• Попередження тепер називає біржу й символ; числа та `status` не змінено; збій запиту — як і раніше в `missing`.
• Наживо (binanceusdm / bybit / okx): історія повна, позначок 0.
• Мій прогін: `make lint` чисто, `make test` — 7118 passed, 3 skipped.

**Критик:** C (sonnet) — approve, блокерів нема; дві дрібниці без дій ([рев'ю](../../reviews/2026-11-07-funding-carry-incomplete.md)).

**Далі:** `owner` — крок завершено. Питань до власника нема.
