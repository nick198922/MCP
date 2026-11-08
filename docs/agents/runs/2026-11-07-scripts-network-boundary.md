**Крок:** постійна картка `standing-improvements` (крок p), пункт 220 (додав координатор): охоронець мережевих імпортів у `scripts/` (інваріант 2, BACKLOG 6.45). Решта пунктів картки заблокована (8a чекає даних сервера).

**Результат:**
• `tests/test_network_boundary.py` сканує `scripts/**/*.py` (AST): 233 файли, 17 імпортують мережеві модулі.
• 14 — у `ALLOWED_SCRIPTS` з причиною (Telegram, GitHub, YouTube, ключі власника, Dukascopy тощо).
• 3 порушення (`hyperliquid_import.py`, `copy_vaults.py`, `research/vault_profile.py`: публічні дані в обхід `Http`) — у `KNOWN_SCRIPT_VIOLATIONS` і BACKLOG 6.45, скрипти не переписувано.
• `make lint` чисто, `make test`: 6972 passed, 3 skipped.

**Критик:** C (sonnet) — approve, блокерів нема; зауваження низькі (дозвіл на файл, а не хост; `import urllib` + `urllib.request.…` не ловиться). [Рев'ю](../../reviews/2026-11-07-scripts-network-boundary.md).

**Далі:** `owner`. Питання власнику: для трьох скриптів — перевести на `Http` чи дозволити з причиною (BACKLOG 6.45).
