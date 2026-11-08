**Крок:** p — постійна картка `standing-improvements`, новий пункт 284 (відкритим лишався лише заблокований 8a).

**Результат:**
• `scripts/assistant_paper_report.py`: щотижневий звіт `assistant_paper` лише з лічильників (угоди, нерозраховані, проміжки, останнє денне рішення), без P&L; БД `mode=ro`.
• Користувацький таймер `tradedesk-assistant-paper-report` (нд 20:15 Київ) + скрипт `scripts/server/30-assistant-paper-report-timer.sh` (без root, за зразком 29; не запускався).
• Сухий прогін на живій БД: 3 методові угоди, 2 відкриті, 1 прострочена без розрахунку, 0 проміжків.
• `make lint` чисто; `make test`: 7622 passed, 3 skipped.

**Критик:** approve (mode C), блокерів нема; зауваження: `OSError` при записі файлу не обробляється, немає тесту успішної гілки Telegram.

**Далі:** `owner` — змерджити й запустити `bash scripts/server/30-assistant-paper-report-timer.sh` як `tradedesk`. У картці лишається один відкритий пункт 8a (чекає даних сервера). Гілка `agent/2026-11-04-library-seller-exhaustion-card` чекає власника.
