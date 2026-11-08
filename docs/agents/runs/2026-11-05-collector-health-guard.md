**Крок:** p, постійна картка standing-improvements (відкритий лише 8a, що чекає сервера) → додано пункт 96 (пропуски даних).

**Результат:**
• Тест-охоронець у `tests/test_collector_health.py`: кожна джоба планувальника має `Spec`, запис у `NOT_CHECKED` або `EXEMPT` з причиною.
• З 25 джоб 4 були без серії: `retention`, `alerts`, `assistant_snapshot`, `ohlcv_hist` — усі в `EXEMPT`; нових `Spec` не потрібно.
• Головне відкрите: `assistant_snapshot` пише файл, а не таблицю — перевірка віку файлу записана в BACKLOG 6.13 (в).
• `make lint && make test`: 4639 passed, 6 skipped.

**Критик:** C — approve ([рев'ю](../../reviews/2026-11-05-collector-health-guard.md)); дрібниця: перемикачі шукаються regex-ом у `config.py`.

**Далі:** owner. Нових питань немає.
