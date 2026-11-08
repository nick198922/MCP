**Крок:** p — постійна картка standing-improvements, новий пункт 169 (BACKLOG 6.46): сирі записи в БД у скриптах.

**Результат:**
• `journal_cancel_tg.py`, `journal_close_tg.py`, `bybit_import.py` пишуть через `Store._write()` (відкат при помилці).
• Тест-охоронець розширено на `scripts/` (виключення з причинами: `copy_vaults.py`, `hyperliquid_import.py:mark_airdrop`, один хибний збіг).
• Нові тести з відмовою UPDATE: транзакція не лишається відкритою (на старому коді падають).
• Різниця лише на збої: в `bybit_import` оновлення size/entry комітиться окремо від закриття (ідемпотентно).
• `make lint` ок, `make test`: 6213 passed, 3 skipped.

**Критик:** C (sonnet) — approve, блокерів нема ([рев'ю](../../reviews/2026-11-06-scripts-store-writes.md)).

**Далі:** owner. У постійній картці лишився тільки 8a (потрібен сервер); `hyperliquid_import.py:mark_airdrop` без відкату — кандидат на наступний пункт.
