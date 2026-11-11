# Змінні оточення скриптів

Довідник назв `TRADEDESK_*`, які читають лише скрипти в `scripts/` (Python — перша таблиця, shell — [друга](#shell-скрипти)) і яких немає в `deploy/env.example`, юнітах чи інших документах (розділ «read by a script, set nowhere, documented nowhere» звіту `uv run python scripts/env_audit.py --scripts`, прогін 07.11.2026). Змінні сервера MCP описано в `deploy/env.example`.

Усі 21 назва мають значення за замовчуванням у коді, тож на сервері скрипти працюють без них: жоден юніт у `deploy/user-units/` їх не задає. Задавати їх варто лише для нестандартних шляхів, тестів чи ручного запуску. Значення нижче взято лише з коду; значень оточення й вмісту файлів тут немає. `~` — домашня тека користувача, від якого запущено скрипт (`Path.home()`).

| Назва | Хто читає | За замовчуванням | Призначення |
|---|---|---|---|
| `TRADEDESK_AGENTS_CACHE_DIR` | `scripts/agents_rm.py` | `~/.cache/tradedesk-agents` | Кеш агентів: друга (після клону) тека, де `agents_rm.py` дозволяє видаляти файли |
| `TRADEDESK_AGENTS_NO_NOTIFY` | `scripts/agents_status.py` | не задано | Будь-яке непорожнє значення: статус друкується в консоль замість надсилання в Telegram (як `--print`) |
| `TRADEDESK_AGENTS_OWNER_ONLY_AT` | `scripts/dashboard.py` | `80` | Поріг тижневого використання плану Claude, %, від якого дашборд показує режим «агенти беруть лише картки власника» (поруч із паузою `TRADEDESK_AGENTS_MAX_USAGE`, 97) |
| `TRADEDESK_AGENTS_STATE` | `scripts/channel_watch.py` | `~/.local/state/tradedesk-agents` | Тека стану агентів, звідки береться `usage.json` (використання плану, не старше години) для очікування при високому ліміті |
| `TRADEDESK_CHAT_MAX_SECONDS` | `scripts/tg_bridge.py` | `1800` | Найдовша відповідь Claude в темі «💬 Claude», с |
| `TRADEDESK_CLAUDE_BIN` | `scripts/channel_watch.py`, `scripts/tg_bridge.py`, `scripts/trader_review.py` | `scripts/claude_run.sh` | Шлях до CLI `claude`; за замовчуванням обгортка, що бере запасний токен, коли логін сплив (з 11.11) |
| `TRADEDESK_CLAUDE_REAL` | `scripts/claude_run.sh`, `scripts/claude_auth.py`, `scripts/server/claude-token.sh` | `~/.local/bin/claude` | Справжній CLI `claude` за обгорткою |
| `TRADEDESK_CLAUDE_TOKEN_FILE` | `scripts/claude_run.sh`, `scripts/claude_auth.py`, `scripts/server/claude-token.sh` | `~/.config/tradedesk/claude-token` | Запасний токен `claude setup-token` (рік, 0600) |
| `TRADEDESK_DASHBOARD_STATE` | `scripts/dashboard.py`, `scripts/journal_refresh.py` | `~/.local/state/tradedesk-dashboard` | Тека стану дашборда; там `journal-refresh.json` (стан оновлення угод) і `journal-refresh.lock` |
| `TRADEDESK_DATA_DIR` | `scripts/agents_status.py`, `scripts/collector_health.py`, `scripts/dashboard.py`, `scripts/journal_trade_tg.py` | `/var/lib/tradedesk-mcp` | Тека даних сервера: база `tradedesk.sqlite`, `assistant/snapshot.json` тощо |
| `TRADEDESK_HL_ADDRESS_FILE` | `scripts/journal_refresh.py` | `/var/lib/tradedesk-mcp/journal/hl-address` | Файл з адресою Hyperliquid власника для імпорту публічних угод у журнал (поза git; вміст тут не наводиться) |
| `TRADEDESK_LIVE_POSITIONS` | `scripts/dashboard.py` | `/var/lib/tradedesk-mcp/assistant/live_positions.json` | Файл стану `scripts/bybit_live.py` (живі позиції Bybit) для вкладки «Позиції» |
| `TRADEDESK_PRFIX_DIR` | `scripts/pr_watch.py` | `~/tradedesk-prfix` | Окремий клон, у якому сторож PR зливає `main` у гілку PR при конфлікті в документації |
| `TRADEDESK_PRIVATE_DIR` | `scripts/channel_watch.py` | `~/private` | Приватна тека поза git: `library/courses/<канал>/…` (пости, live, відео) і `tg-inbox/` |
| `TRADEDESK_REPO` | `scripts/dashboard.py` | `~/tradedesk-mcp` | Основний клон: git-гілки для дашборда, `events_seed.json`, запуск `scripts/journal_refresh.py` |
| `TRADEDESK_REVIEW_DIR` | `scripts/trader_review.py` | `~/tradedesk-review` | Окремий клон для рев'ю PR демо-трейдера й запобіжників |
| `TRADEDESK_REVIEW_MODEL` | `scripts/trader_review.py` | `opus` | Модель Claude для рев'ю (`tradedesk-trader-review`) |
| `TRADEDESK_UV_BIN` | `scripts/channel_watch.py`, `scripts/tg_bridge.py` | `~/.local/bin/uv` | Шлях до `uv` |
| `TRADEDESK_WATCH_CHANNEL` | `scripts/channel_watch.py` | `DataHawks` | Канал Telegram за замовчуванням у функціях скрипта, коли канал не передано (теки live/відео, розбір сторінки) |
| `TRADEDESK_WATCH_CHANNELS` | `scripts/channel_watch.py` | усі канали словника `WATCHED` у скрипті, через кому | Список каналів, які обходить `run` |
| `TRADEDESK_WATCH_DIR` | `scripts/channel_watch.py` | `~/tradedesk-watch` | Окремий клон, у якому `claude -p` розбирає нові пости |
| `TRADEDESK_WATCH_MODEL` | `scripts/channel_watch.py` | `opus` | Модель Claude для розбору постів |
| `TRADEDESK_YT_PROXY` | `scripts/yt_transcript.py` | не задано (без проксі) | URL http(s)-проксі для запитів до YouTube, коли той блокує IP сервера. Може містити облікові дані: лише в оточенні, не в git |

Призначення без «не з'ясовано»: для всіх 21 назви воно видно з коду або docstring скрипта.

## Shell-скрипти

Назви, які читають лише shell-скрипти (`$NAME` / `${NAME:-…}`), не сервер і не Python-скрипти, і які ніде не задано й не описано (прогін 07.11.2026: таких 31, задано в юніті чи скрипті або описано в документах 11, лишилось 20). Значення за замовчуванням — із `${NAME:-…}` у коді; жоден юніт їх не задає. Секунди — с, відсотки — пункти тижневого використання плану Claude. «Поруч із клоном» — тека з такою назвою поряд із клоном, з якого запущено скрипт (`$(dirname "$REPO")`).

| Назва | Хто читає | За замовчуванням | Призначення |
|---|---|---|---|
| `TRADEDESK_AGENTS_FAIL_MAX_WAIT_SECONDS` | `scripts/agents_loop.sh` | `14400` | Найдовша пауза після збоїв поспіль, с: пауза подвоюється від `TRADEDESK_AGENTS_GAP_SECONDS` до цієї межі |
| `TRADEDESK_AGENTS_GAP_SECONDS` | `scripts/agents_loop.sh` | `300` | Пауза між кроками конвеєра й перша пауза після збою, с |
| `TRADEDESK_AGENTS_GITHUB_WAIT_SECONDS` | `scripts/agents_loop.sh` | `300` | Інтервал повторної перевірки, поки GitHub недоступний, с |
| `TRADEDESK_AGENTS_IDLE_WAIT_SECONDS` | `scripts/agents_loop.sh` | `1800` | Очікування, коли роботи немає, с (або до злиття гілки агентів, якщо раніше) |
| `TRADEDESK_AGENTS_LOUD` | `scripts/nightly_agents.sh` | не задано | `1`: рутинні результати кроку (`idle`, `continues`, `parked`, `unfinished`) знову йдуть у Telegram, а не лише в `history.tsv` і статус |
| `TRADEDESK_AGENTS_OWNER_PACE_CAP` | `scripts/agents_loop.sh` | `85` | Тижневий %, від якого навіть картки власника чекають на рівномірний графік витрат |
| `TRADEDESK_AGENTS_REPORT_HOUR` | `scripts/agents_loop.sh` | `9` | Година (`date +%H` сервера), від якої раз на добу запускається щоденна перевірка лікаря |
| `TRADEDESK_AGENTS_REVIEW_WAIT_SECONDS` | `scripts/agents_loop.sh` | `1800` | Очікування, коли `TRADEDESK_AGENTS_MAX_OPEN` гілок агентів чекають на перегляд власника, с |
| `TRADEDESK_AUDIT_CLONE` | `scripts/agents_audit.sh` | `tradedesk-audit` поруч із клоном | Окремий клон тижневого аудиту |
| `TRADEDESK_AUDIT_MAX_SECONDS` | `scripts/agents_audit.sh` | `10800` | Найдовша сесія тижневого аудиту, с |
| `TRADEDESK_AUDIT_MAX_WEEK_PCT` | `scripts/agents_audit.sh` | `12` | Скільки пунктів тижневого ліміту може витратити аудит; перевірка кожні 5 хв, понад межу сесію зупинено |
| `TRADEDESK_DASHBOARD_PORT` | `scripts/dashboard-open.sh` | `8090` | Локальний порт SSH-тунелю з Mac до дашборда (на сервері дашборд завжди на `127.0.0.1:8090`) |
| `TRADEDESK_DOCTOR_CLONE` | `scripts/agents_doctor.sh` | `tradedesk-doctor` поруч із клоном | Окремий клон лікаря |
| `TRADEDESK_DOCTOR_DAILY_MODEL` | `scripts/agents_doctor.sh` | `sonnet` | Модель Claude для щоденної перевірки лікаря (на збої — `TRADEDESK_DOCTOR_MODEL`, `opus`) |
| `TRADEDESK_DOCTOR_MAX_PER_DAY` | `scripts/agents_loop.sh` | `4` | Скільки запусків лікаря на збої за добу (щоденна перевірка не рахується) |
| `TRADEDESK_DOCTOR_MAX_SECONDS` | `scripts/agents_doctor.sh` | `3600` | Найдовша сесія лікаря, с |
| `TRADEDESK_IDLE_DOCTOR_GAP_SECONDS` | `scripts/agents_loop.sh` | `10800` | Лікар у режимі простою (після двох порожніх кроків поспіль) — не частіше, ніж раз на стільки секунд |
| `TRADEDESK_SERVER` | `scripts/dashboard-open.sh`, 5 скриптів `scripts/mac/*.sh`, 49 скриптів `scripts/server/*.sh` | `169.58.169.184` | Адреса сервера для `ssh tradedesk@…`, коли скрипт запускають з Mac |
| `TRADEDESK_STATUS_EVERY_SECONDS` | `scripts/agents_loop.sh` | `7200` | Період тихого статусу проєкту в Telegram (`scripts/agents_status.py`), с |
| `TRADEDESK_STATUS_QUIET` | `scripts/agents_loop.sh` | `23-8` | Тихі години без статусу, «від-до» за годинником сервера |

Призначення без «не з'ясовано»: для всіх 20 назв воно видно з коду або коментарів скрипта.

Назви з першої таблиці, які читають і shell-скрипти: `TRADEDESK_AGENTS_OWNER_ONLY_AT` (`agents_loop.sh`, `nightly_agents.sh`, той самий `80`), `TRADEDESK_AGENTS_NO_NOTIFY` (`agents_notify.sh`), `TRADEDESK_AGENTS_CACHE_DIR` (`nightly_agents.sh`, той самий шлях), `TRADEDESK_CLAUDE_BIN` (`nightly_agents.sh`, `agents_doctor.sh`, `agents_audit.sh`: за замовчуванням теж `scripts/claude_run.sh`).

## Як це перевіряє аудит

- `env_audit.py --scripts` вважає назву описаною, якщо вона стоїть у цьому файлі у зворотних лапках (звичайний текст не рахується); тож розділ «read by a script, set nowhere, documented nowhere» для 21 назви першої таблиці порожній. Для назв сервера (`src/tradedesk/`) цей файл не рахується: їхній документ — `deploy/env.example`.
- Назви, які читають лише shell-скрипти, аудит у своєму звіті не перелічує (правило читання — AST Python), тож таблицю «Shell-скрипти» звірено вручну функціями самого `env_audit.py` (`names_shell_read`, `names_set`). Див. [зміни 06.11](changes/2026-11-06-env-audit-scripts.md).
