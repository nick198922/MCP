**Крок:** постійна картка standing-improvements (крок p): відкритих пунктів лишався тільки 8a (чекає сервера), додано пункт 105 з BACKLOG 6.9.
• Пункт 105: прямі тести правил виходу carry.

**Результат:**
• `tests/test_analytics_carry_rules_exits.py`: 40 тестів для `carry_basis`, `carry_exit_due`, `spread_stop_hit`, `spread_exit_due`; еталони пораховані вручну, межі порогів, `None`, обидва напрями.
• Помилок не знайдено, `src/` не змінено, числа форвардів ті самі.
• BACKLOG 6.9: лишилось 93 із 1998 (було 97).
• `make lint` і `make test` чисті: 4752 passed, 3 skipped.

**Критик:** C (sonnet): approve, [рев'ю](../../reviews/2026-11-05-carry-rules-exit-tests.md); дрібниця без блокування — тести спираються на структуру `dict` з `carry_open` / `spread_open`.

**Далі:** `owner`. Інших гілок у черзі: `agent/2026-11-04-library-seller-exhaustion-card` чекає власника. Відкритих питань немає; на картці лишився 8a (потрібен сервер), тож наступного разу її знову треба поповнити.
