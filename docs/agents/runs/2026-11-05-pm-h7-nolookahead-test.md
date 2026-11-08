**Крок:** p (картка standing-improvements, лишався один заблокований пункт 8a) → додано пункт 119 із «Як поповнювати» (рев'ю «не блокує»).
**Результат:**
• Два тести проти підглядання в стадії 0 pm-h7: `load_rows` на БД без `pm_gap_outcome`; `replay` ігнорує бари ≥ `REPLAY_HI`.
• Знахідка: authorizer-охоронець SQLite не бачить `JOIN … USING` на `pm_gap_outcome`; новий тест його ловить.
• Код скрипта не змінено. `make lint` чистий, `make test`: 4781 passed, 3 skipped.
**Критик:** approve ([рев'ю](../../reviews/2026-11-05-pm-h7-nolookahead-test.md)); мінор — нумерація 119 стоїть між 56 і 57 (косметика).
**Далі:** owner. Нових питань немає. Гілка agent/2026-11-04-library-seller-exhaustion-card чекає власника.
