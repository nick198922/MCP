**Крок:** p — постійна картка `standing-improvements` (пункт 237); пункти 1–7 карток `Порядок: 0` чекають часу, даних чи власника.

**Результат:**
• `vault_profile._post` більше не кладе адресу в `Evidence.args` (лише `type`), тест додано
• Докстрінги `vault_watch_tg.py` і `hyperliquid_import.py` виправлено (`uv run`, ≤ 100 символів); перший рядок `--help` став коротшим
• `make lint` чисто, `make test`: 7043 passed, 3 skipped

**Критик:** C (sonnet) — approve, [рев'ю](../../reviews/2026-11-07-review-leftovers-vault.md).

**Далі:** `owner`. Питань нових немає; відкрите Q-20261106-1 чекає на вас.
