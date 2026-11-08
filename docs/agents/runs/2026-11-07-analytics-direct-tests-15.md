**Крок:** постійна картка standing-improvements (крок p), пункт 252: технічний борг, BACKLOG 6.9.

**Результат:**
• Прямі тести для 9 функцій `analytics/` (`tests/test_analytics_direct_15.py`, 20 тестів): `npdf`, `wma`, `rolling`, `atm_strike`, `distance`, `mark_price`, `primary_early_payout`, обидві `num`.
• `src/` не змінено; помилок, що міняють числа, немає. Три особливості описано у змінах: `OverflowError` на гігантському int, `ZeroDivisionError` при spot 0, `mark_price` дає 0.0 без `value_usd`.
• `make lint` чистий, `make test`: 7139 passed, 3 skipped.

**Критик:** C (sonnet) — approve; два зауваження низької важливості, не блокують.

**Далі:** `owner`. Відкрите питання лишається одне (Q-20261106-1). Невиконаний пункт картки 8a чекає даних сервера.
