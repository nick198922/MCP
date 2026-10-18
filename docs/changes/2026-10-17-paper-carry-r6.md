### Паперовий carry: інтервал funding у живому фіді (R6)

- `current_funding` (`src/tradedesk/tools/perps.py`) додає `interval_source`: `venue` (біржа повідомила інтервал), `default` (список інтервалів Binance `fundingInfo` прочитано, символу в ньому нема, тобто стандартні 8h) або `assumed` (джерела інтервалу немає, 8h — припущення).
- Живий фід (`carry_paper_feed.py`) при `assumed` більше не передає рушію вгаданих 8h: `interval_hours = None` (рушій бере інтервал з історії до `last`), у циклі лишається `provider_missing` для `funding_intervals:<venue>`.
- TTL кешу `Exchanges.funding_intervals` зменшено з 1 год до 5 хв: Binance перемикає 8h↔4h при екстремальному funding, а застарілий інтервал після 4h→8h тримав би позицію.
- Рушій не змінювався: `interval_hours` з `fundingInfo` фід передавав і раніше. R6 додав лише `None` замість вгаданих 8h (рушій бере інтервал з історії) і коротший TTL; тести підтверджують, що `_missing_payments` і перевірка розриву коректно йдуть за інтервалом з фіда в обох напрямках перемикання. Зміну TTL окремим тестом не покрито.
- Тести: `tests/test_carry_paper_live.py` (інтервал із fundingInfo, збій fundingInfo, перемикання на 4h у циклі, `_missing_payments` для обох перемикань).
- Адмінський скрипт `scripts/server/17-paper-carry-report-timer.sh`: таймер systemd для `scripts/paper_carry_report.py`, неділя 20:00 за Києвом (запускає власник через `nick`). Звіт пише `/var/lib/tradedesk-mcp/paper/REPORT.md` (поза git-копією, щоб `git pull --ff-only` не ламався); надсилання в Telegram скрипт звіту не робить.
- Прапорець `TRADEDESK_PAPER_CARRY_ENABLED` не змінювався.
