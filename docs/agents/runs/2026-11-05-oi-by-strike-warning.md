**Крок:** p, постійна картка standing-improvements (Порядок 1); картка structural-edges-depth (8f/8k/8l) чекає вікна потужності, тож узято новий пункт 108.

**Результат:**
• `oi_by_strike`: попередження про різні форварди тепер за `math.isclose(rel_tol=1e-6)`, а не за точною рівністю float (шум округлення `underlying_price` давав хибне застереження).
• Числа `notional_usd`, смуг, `distance_pct` не змінились; тест на 1e-9 / 1e-4 / 1.8 %.
• `make lint` чисто, `make test`: 4756 passed, 3 skipped.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-05-oi-by-strike-warning.md).

**Далі:** `owner`. Відкритий лише 8a (чекає сервера). Гілка `agent/2026-11-04-library-seller-exhaustion-card` чекає власника.
