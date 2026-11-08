# Рев'ю: пункт 234, змінні shell-скриптів і env_audit (режим C)

Вердикт: **approve**

Перевірено сам: `make lint` (чисто), `uv run pytest -q tests/test_env_audit.py` (9 passed), `uv run python scripts/env_audit.py --scripts` (усі списки 0, код виходу 0), `git status` (змінені лише env_audit.py, тест, ENV-SCRIPTS.md, BACKLOG, HANDOFF, картка, новий файл у docs/changes/).

Заборонені шляхи (`scripts/nightly_agents.sh`, `.claude/`, `deploy/`, `execution/`) не змінено.

## Знахідки

1. (інфо) Зміна аудиту правильна: `names_in_backticks` бере лише code spans і застосовується лише до `docs/ENV-SCRIPTS.md`. Решта документів і секція сервера не зачеплені, тест це перевіряє (згадка в прозі не рахується, шаблон `NAME_*` не рахується, серверна секція не зараховує файл). Тести падали б при поломці.
2. (інфо) Вибірка із 18 назв звірена з shell-кодом, значення за замовчуванням збігаються: FAIL_MAX_WAIT 14400, GAP 300, OWNER_PACE_CAP 85, REVIEW_WAIT 1800, REPORT_HOUR 9, DOCTOR_MAX_PER_DAY 4, STATUS_EVERY 7200, STATUS_QUIET 23-8, IDLE_DOCTOR_GAP 10800, AUDIT_MAX_SECONDS 10800, AUDIT_MAX_WEEK_PCT 12, DOCTOR_DAILY_MODEL sonnet, DOCTOR_MAX_SECONDS 3600, DASHBOARD_PORT 8090, AUDIT_CLONE, AGENTS_LOUD (=1), CLAUDE_BIN (`claude`, не `~/.local/bin/claude`). Читачі збігаються.
3. (низька) Рядок `TRADEDESK_SERVER` і `TRADEDESK_AGENTS_IDLE_WAIT/GITHUB_WAIT` у вибірці не перевіряв поштучно (лічильники «5 і 49 скриптів» теж); це довідкові числа без впливу на код.
4. Секретів і значень оточення немає: лише назви та значення за замовчуванням з коду.

Блокерів немає.
