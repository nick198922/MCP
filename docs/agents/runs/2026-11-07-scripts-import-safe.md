**Крок:** постійна картка вдосконалень (крок p); єдиний відкритий пункт 8a заблокований, тож додано пункт 221 — безпечність імпорту `scripts/`.

**Результат:**
• `tests/test_scripts_import_safe.py` (AST, без мережі): 224 скрипти, 2 порушення — `verify_demo_trend_veto.py`, `explore_us_open_calls.py` виконували все на імпорті; тепер під `__main__`, вивід не змінився
• 5 винятків у `ALLOWED` з причиною; `RUNS_AT_MODULE_LEVEL` у `tests/test_scripts_import.py` спорожнено
• `make lint` і `make test`: 6982 passed, 3 skipped

**Критик:** C (sonnet) — approve. Не блокує: гілка `else` охоронця не перевіряється; `os.environ.get` у безпечних викликах — [рев'ю](../../reviews/2026-11-07-scripts-import-safe.md).

**Далі:** `owner`. BACKLOG 6.51, [зміни](../../changes/2026-11-07-scripts-import-safe.md). Нових питань немає.
