# Походження фікстур без дати в імені

Правило (`CLAUDE.md`): нові зразки зберігати з позначкою дати й джерела. Більшість файлів має дату `YYYY-MM-DD` в імені або поле `_source` / `captured_at` усередині; перевірка — `scripts/fixtures_check.py`. Тут лише файли без жодної з цих ознак. Дата й коміт узяті з `git log --diff-filter=A`, опис — з повідомлення коміту або з поля `_note` самого файлу; нічого не додано від себе.

| Файл | Додано (git) | Коміт | Джерело / що відомо |
|---|---|---|---|
| `deribit_options_sample.json` | 2026-10-10 | `8e5aa143` «Phase 0-1: core, Deribit/Hyperliquid/ccxt providers…» | Поле `_note`: урізана реальна відповідь `public/get_book_summary_by_currency?currency=BTC&kind=option`, знята 2026-10-10 ≈13:07 UTC. |
| `fear_greed_sample.json` | 2026-10-10 | `d3ff031c` «Phase 1.6: sentiment_fear_greed (alternative.me)» | За комітом: формат alternative.me `/fng/`, «anchored to NOW», 30 денних значень від extreme_fear до extreme_greed. Чи це реальний знімок, чи складений вручну, у коміті не сказано. Реальний знімок того ж API: `alternative_me_fng_live_2026-10-10.json`. |
| `forexfactory_calendar_sample.json` | 2026-10-18 | `0932a10a` «Macro calendar tool (macro_calendar), part A of IDEA-20261017-macro» | Поле `_note`: **складений вручну, не реальний знімок** (поля за документацією `ff_calendar_thisweek/nextweek.json`, дати й числа вигадані для тестів). Реальний знімок: `forexfactory_thisweek_2026-10-18.json`. |
