**Крок:** p, постійна картка (`standing-improvements`, Порядок 1): картка «пошук прибутку» (Порядок 0) вичерпана — п. 2 і 3 дали «нема що брати», п. 4–5 без ✓; додано й виконано пункт 332.
**Результат:**
• `test_demo_trader.py`, `test_rules_pretrade_tg.py`, `test_journal_trade_tg.py` переведено на спільну фікстуру `migrated_db`; копії схеми прибрано.
• Час setup: 7.58 → 0.30 с і 5.18 → 0.35 с (два файли); `test_demo_trader.py` без виграшу в часі (шаблон уже був), лише менше дубльованого коду.
• Кількість тестів і асерти не змінено; `make lint` чисто, `make test`: 9622 passed, 3 skipped (мої власні запуски).
**Критик:** C (sonnet) — approve, блокерів немає ([рев'ю](../../reviews/2026-11-12-test-store-template-3.md)).
**Далі:** `owner`. Наступні кандидати названо в change-файлі: `test_tool_failure_extra_8.py`, `test_journal_cancel_tg.py`. Нових питань нема; Q-20261111-1 (Wikimedia) досі чекає.
