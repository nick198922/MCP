**Крок:** p, постійна картка `IDEA-20261102-standing-improvements` (відкритих пунктів < 3, додано пункт 91 із джерела «технічний борг»).

**Результат:**
• 6 локальних копій `rss_mb` у `scripts/verify_*` замінено на `common_helpers.rss_mb`; вивід скриптів не змінився.
• Лишилась 1 копія: `verify_carry_a_entry_ban.py` (повертає int, хелпер змінив би формат виводу).
• Тест `tests/test_common_helpers.py` охоплює ще 6 модулів.
• `make lint` чистий, `make test`: 4632 passed, 6 skipped.

**Критик:** C, approve ([рев'ю](../reviews/2026-11-05-rss-mb-dedupe-3.md)); зауваження, що не блокує: аліас `rss_mb = s7.rss_mb` у `verify_deviation_weekly_trend.py` — не копія.

**Далі:** `owner`; нових питань немає. Інші картки: відкриті пункти чекають часу, даних або власника.
