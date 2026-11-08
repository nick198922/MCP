**Крок:** p, постійна картка (`Порядок: 1`); пункт 235 — закрити три відомі порушення інваріанта 2 у `scripts/` (BACKLOG 6.45). Решта карток із пріоритетом чекає часу, даних сервера чи власника.

**Результат:**
• `copy_vaults.py`, `research/vault_profile.py`, `hyperliquid_import.py` (усі живі: таймери, `journal_refresh`, `hl-import.sh`) переведено на `core.http.Http`; `KNOWN_SCRIPT_VIOLATIONS` порожній.
• Вивід скриптів не змінено; 4xx більше не ретраїться; нові тести `tests/test_scripts_http.py` (9, MockTransport).
• Два `Http(...)` на рівні модуля додано в `ALLOWED` охоронця імпорту (конструктор без I/O).
• `make lint` чисто; повний `make test`: 7041 passed, один збій охоронця імпорту — виправлено, файл тепер 10 passed.

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-07-scripts-network-violations.md); три зауваження без блокування (адреса гаманця в `Evidence.args`, `python3` у докстрінгу `vault_watch_tg`, довгий докстрінг).

**Далі:** `owner`. Власнику перевірити наживо: `journalctl --user -u tradedesk-copy-vaults -n 20` (не «run skipped») і `scripts/server/hl-import.sh <адреса>` (те саме число угод). Нових питань немає.
