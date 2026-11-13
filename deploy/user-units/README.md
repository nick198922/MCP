# User timers (без root)

Сюди кладуться systemd-юніти `*.service` + `*.timer`, що працюють від користувача `tradedesk` (як MCP і агенти).
Після злиття в `main` сервер за годину сам їх підхоплює: `tradedesk-update` → `scripts/user_units_sync.sh`
(новий таймер вмикається, видалений з git — вимикається). Root і власник не потрібні.

Шаблон:

```ini
# deploy/user-units/tradedesk-example.service
[Unit]
Description=tradedesk: example
[Service]
Type=oneshot
WorkingDirectory=/home/tradedesk/tradedesk-mcp
ExecStart=/home/tradedesk/.local/bin/uv run python scripts/example.py
TimeoutStartSec=5min
NoNewPrivileges=true
```

```ini
# deploy/user-units/tradedesk-example.timer
[Timer]
OnCalendar=*-*-* 06:00:00 Europe/Kyiv
Persistent=true
[Install]
WantedBy=timers.target
```

Дані — у `/var/lib/tradedesk-mcp/<назва>/` (каталог належить `tradedesk`, створювати сам скрипт). Журнал:
`journalctl --user -u tradedesk-example`.

## Знімок ladder Bybit щотижня

`tradedesk-ladder-weekly.timer` / `.service`: щонеділі о 12:00 за Києвом (пропущену неділю доганяє після
перезавантаження) запускає `scripts/paper_ladder_snapshot.py --out-dir /var/lib/tradedesk-mcp/ladder` і пише
`/var/lib/tradedesk-mcp/ladder/<дата UTC>-bybit.json`. Публічний ендпоінт Bybit, без ключів і ордерів; файли поза
git. Звіт чекпоінта (`scripts/checkpoint_report.py`) читає їх разом із `docs/paper/ladder/` (інший каталог —
змінна `TRADEDESK_LADDER_DIR`); та сама дата в обох місцях — один запис (з `docs/paper/ladder/`). Перемикача немає:
таймер ставить і вмикає `scripts/user_units_sync.sh`, root не потрібен. Перевірити:
`systemctl --user list-timers tradedesk-ladder-weekly.timer`, `ls /var/lib/tradedesk-mcp/ladder/`.
