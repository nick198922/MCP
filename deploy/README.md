# Deploy tradedesk-mcp (Фаза 2.8)

Серверна мета: збирач працює 24/7, HTTP-транспорт доступний лише з довіреного каналу (VPN/Tailscale або TLS+bearer-токен за реверс-проксі). HTTP MCP у коробці **без автентифікації** — не публікуй порт 8000 назовні без фронта.

## Варіант A — Docker Compose (рекомендовано)

Потрібно: Docker ≥ 24 і docker compose plugin (Linux VPS: Hetzner CX11/22, DO 1-2 vCPU 2 GB — цього достатньо).

```bash
git clone https://github.com/nick198922/MCP tradedesk-mcp
cd tradedesk-mcp
cp deploy/env.example .env    # відредагуй за потреби
mkdir -p data

docker compose up -d --build
docker compose logs -f tradedesk-mcp
```

Що відбудеться:
- Образ збирається з `Dockerfile` (uv builder → slim runtime, non-root `tradedesk` uid 10001).
- `./data` монтується в `/data`; SQLite живе в `/data/tradedesk.sqlite`.
- Збирач вмикається (`TRADEDESK_COLLECTOR_ENABLED=1`), пише історію кожні 5 хв / 1 год, retention раз на добу.
- Порт 8000 слухає **лише 127.0.0.1** на хості — усе зовнішнє проходить через reverse-proxy.

### Reverse-proxy з Caddy

Приклад `/etc/caddy/Caddyfile`:

```caddy
mcp.example.com {
    @auth {
        header Authorization "Bearer YOUR_LONG_RANDOM_TOKEN"
    }
    handle @auth {
        reverse_proxy 127.0.0.1:8000
    }
    respond "Unauthorized" 401
}
```

Caddy сам підніме TLS від Let's Encrypt. Токен тримай **довгим** (32+ байти з `openssl rand -base64 32`). Пара MCP-клієнтів передає `Authorization: Bearer …` у заголовках.

### Бекапи

```bash
# ad-hoc
docker compose exec tradedesk-mcp bash -c "/app/deploy/backup-sqlite.sh"
# cron (host): щодня о 03:00
crontab -e
# 0 3 * * * cd /opt/tradedesk-mcp && docker compose exec -T tradedesk-mcp bash /app/deploy/backup-sqlite.sh >> /var/log/tradedesk-backup.log 2>&1
```

Скрипт `deploy/backup-sqlite.sh` використовує `sqlite3 .backup`, тому колектор може писати під час копіювання (WAL лишається консистентним). Файли gzip'ються і зберігаються в `/data/backups/`, старші за `BACKUP_KEEP_DAYS` (30 за замовчуванням) видаляються.

Off-site копія: раз на добу `rclone copy /opt/tradedesk-mcp/data/backups remote:tradedesk-backups` або `restic`.

### Оновлення

```bash
cd /opt/tradedesk-mcp && git pull && docker compose build && docker compose up -d
```

## Варіант B — systemd на bare VPS без Docker

```bash
sudo useradd --system --home /opt/tradedesk-mcp --shell /sbin/nologin tradedesk
sudo mkdir -p /opt/tradedesk-mcp /var/lib/tradedesk-mcp
sudo chown -R tradedesk:tradedesk /opt/tradedesk-mcp /var/lib/tradedesk-mcp

sudo -u tradedesk git clone https://github.com/nick198922/MCP /opt/tradedesk-mcp
cd /opt/tradedesk-mcp
sudo -u tradedesk uv sync --no-dev

sudo install -m 0600 -o root -g root deploy/env.example /etc/tradedesk-mcp.env
sudo editor /etc/tradedesk-mcp.env   # хоча б TRADEDESK_DB_PATH=/var/lib/tradedesk-mcp/tradedesk.sqlite
sudo cp deploy/tradedesk-mcp.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now tradedesk-mcp
sudo systemctl status tradedesk-mcp
journalctl -u tradedesk-mcp -f
```

У systemd-unit `ReadWritePaths=/var/lib/tradedesk-mcp` — туди і повинен указувати `TRADEDESK_DB_PATH`. Інакше ProtectSystem=strict заблокує запис.

## Що лишилось зробити вручну (поки не автоматизовано)

- **TLS + bearer** — у коробці нема, тож Caddy/Nginx + ротація токена під вашу відповідальність (див. 2.9 у BACKLOG).
- **/healthz** — MCP SDK streamable-http не експонує окремий ендпойнт, тому healthcheck у Docker/Compose просто перевіряє, що порт підв'язаний. Коли SDK додасть endpoint, замінити на HTTP-перевірку.
- **Моніторинг** — як мінімум `docker compose logs` або journalctl у tail; метрики Prometheus — опційно у 2.10.
