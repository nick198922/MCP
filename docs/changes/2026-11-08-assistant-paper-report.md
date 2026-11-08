# 2026-11-08 — щотижневий звіт `assistant_paper` (лише лічильники)

Пункт 284 [IDEA-20261102-standing-improvements](../ideas/IDEA-20261102-standing-improvements.md).

- **Новий скрипт** `scripts/assistant_paper_report.py`: угод методу (усього й нових за 7 днів), чинних, експірованих і нерозрахованих, проміжки понад добу без чинної угоди, останнє денне рішення з файлу стану. **Без P&L, CLV і результатів.** Лічильники ті самі, що в `scripts/checkpoint_report.py`: скрипт бере його читачі (`mode=ro`, без мережі) і чисту функцію `checkpoint.assistant_paper_forward`, тож обидва звіти рахують однаково.
- **Вивід:** `--out`, інакше `$TRADEDESK_ASSISTANT_PAPER_REPORT`, інакше `/var/lib/tradedesk-mcp/paper/assistant_paper_REPORT.md` (поза git); `--dry-run` лише друкує, `--telegram` шле в тему `agents`. Якщо БД чи таблицю `rec_paper` не прочитано, звіт це називає й завершується з кодом 1.
- **Таймер** — неділя 20:15 (Київ), user-юніт `deploy/user-units/tradedesk-assistant-paper-report.*`. Він нічого не робить, доки власник не запустить `bash scripts/server/30-assistant-paper-report-timer.sh` від `tradedesk` (sudo не потрібен). Скрипт покаже попередній перегляд, увімкне прапорець і одразу надішле один звіт; `… off` вимикає.
- **Тести** `tests/test_assistant_paper_report.py`: тимчасова БД з продакшн-міграцій (P&L 12345.678 і зайве поле стану в текст не потрапляють, БД не змінюється), порожня БД, відсутні БД і таблиця, `--dry-run`, збій надсилання в Telegram.
