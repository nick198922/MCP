# Змінні оточення скриптів

Довідник назв `TRADEDESK_*`, які читають лише Python-скрипти в `scripts/` і яких немає в `deploy/env.example`, юнітах чи інших документах (розділ «read by a script, set nowhere, documented nowhere» звіту `uv run python scripts/env_audit.py --scripts`, прогін 07.11.2026). Змінні сервера MCP описано в `deploy/env.example`.

Усі 21 назва мають значення за замовчуванням у коді, тож на сервері скрипти працюють без них: жоден юніт у `deploy/user-units/` їх не задає. Задавати їх варто лише для нестандартних шляхів, тестів чи ручного запуску. Значення нижче взято лише з коду; значень оточення й вмісту файлів тут немає. `~` — домашня тека користувача, від якого запущено скрипт (`Path.home()`).

| Назва | Хто читає | За замовчуванням | Призначення |
|---|---|---|---|
| `TRADEDESK_AGENTS_CACHE_DIR` | `scripts/agents_rm.py` | `~/.cache/tradedesk-agents` | Кеш агентів: друга (після клону) тека, де `agents_rm.py` дозволяє видаляти файли |
| `TRADEDESK_AGENTS_NO_NOTIFY` | `scripts/agents_status.py` | не задано | Будь-яке непорожнє значення: статус друкується в консоль замість надсилання в Telegram (як `--print`) |
| `TRADEDESK_AGENTS_OWNER_ONLY_AT` | `scripts/dashboard.py` | `80` | Поріг тижневого використання плану Claude, %, від якого дашборд показує режим «агенти беруть лише картки власника» (поруч із паузою `TRADEDESK_AGENTS_MAX_USAGE`, 97) |
| `TRADEDESK_AGENTS_STATE` | `scripts/channel_watch.py` | `~/.local/state/tradedesk-agents` | Тека стану агентів, звідки береться `usage.json` (використання плану, не старше години) для очікування при високому ліміті |
| `TRADEDESK_CHAT_MAX_SECONDS` | `scripts/tg_bridge.py` | `1800` | Найдовша відповідь Claude в темі «💬 Claude», с |
| `TRADEDESK_CLAUDE_BIN` | `scripts/channel_watch.py`, `scripts/tg_bridge.py`, `scripts/trader_review.py` | `~/.local/bin/claude` | Шлях до CLI `claude` |
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

## Що не охоплено

- 20 назв, які читають лише shell-скрипти агентів (`agents_loop.sh`, `agents_doctor.sh`, `agents_audit.sh`, `scripts/mac/` тощо: `AGENTS_*_WAIT_SECONDS`, `DOCTOR_*`, `AUDIT_*`, `STATUS_*` …), сюди не входять: `env_audit.py --scripts` читає лише Python (AST). Див. [зміни 06.11](changes/2026-11-06-env-audit-scripts.md).
- `env_audit.py` не враховує цей файл як документ, тож його звіт і далі показує ці 21 назву.
