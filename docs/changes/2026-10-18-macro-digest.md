# Макро-дайджест у Telegram (картка macro, пункт A)

- `src/tradedesk/analytics/macro_digest.py` — чисті функції: `week_bounds`, `format_week_digest` (огляд тижня Пн–Нд за Києвом: час, країна, подія, прогноз, попереднє), `due_reminders` / `format_reminder` (події, що стартують у вікні, за замовчуванням 60 хв; події із сіду пропускаються, бо їх час приблизний). Текст стрічки недовірений: розмітку вирізано.
- `scripts/macro_digest.py` — режими `week` і `remind`, `--dry-run` (друк замість відправки), `--out`, `--data-dir`, `--window-min`. Дані: наявний провайдер ForexFactory (`core.http`) + сід через `build_macro_calendar`; відправка через наявний `scripts/tg_send.py` у тему `alerts`. Нових секретів немає.
- Історія прогнозів: `week` зберігає рядки тижневої стрічки в `/var/lib/tradedesk-mcp/macro/ff_thisweek_YYYY-MM-DD.json` (≈20 КБ на тиждень); `remind` веде `reminders_sent.json` (до 500 ключів) проти дублів.
- `scripts/server/18-macro-digest-timer.sh` — для власника: таймери `tradedesk-macro-week` (неділя 20:00 Київ) і `tradedesk-macro-remind` (кожні 15 хв). Не запускався.
- Тести: `tests/test_macro_digest.py` (фікстура `forexfactory_thisweek_2026-10-18.json` + синтетичні рядки). MCP-інструментів не додано, PARITY/README не змінено.
- Обмеження: на тиждень 19–25.10 у реальній фікстурі немає подій високого впливу (США/ECB/BoJ/BoE), тож огляд для неї порожній. Якщо `nextweek` ще віддає 404, понеділкові події беруться з `thisweek` (FF тиждень починається в неділю).
