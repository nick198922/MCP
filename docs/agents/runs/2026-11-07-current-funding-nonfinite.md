**Крок:** p, картка `standing-improvements` (Порядок 1), новий пункт 257 — залишки BACKLOG 6.52 (точність даних). Інші картки власника заблоковані часом чи рішеннями власника.

**Результат:**
• Поточний funding (`current_funding`, `hl_current_funding`, `perps_funding_compare`) відкидає NaN / inf і називає символи / біржі в `warnings`; раніше `"abc"` валив `perps_funding_compare`.
• `option_xvenue.num` ловить `OverflowError` (тест).
• Коментар `schema.py` про `rank_month` виправлено; новий змістовний тест `tracked_traders`.
• Побічно: числовий рядок у масовому funding ccxt тепер розбирається, а не пропускається.

**Критик:** approve (C, `docs/reviews/2026-11-07-current-funding-nonfinite.md`), блокерів нема; дрібниці: Deribit / HL пропускають `"abc"` без попередження, `missing` не заповнюється.

**Перевірка:** `make lint` чисто, `make test` 7214 passed, 3 skipped.

**Далі:** `owner`. Лишилось у BACKLOG 6.52: покриття фіду carry для неутримуваних символів, `dex.num`, NaN у mark / index цінах.
