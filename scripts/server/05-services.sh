#!/bin/bash
# Step 5 of the server move: systemd services. Run as root:
#   ssh root@SERVER 'bash -s' < scripts/server/05-services.sh
# - tradedesk-mcp: MCP server (streamable HTTP on 127.0.0.1:8000) with the collector, always on
# - tradedesk-update (02:15): git pull + uv sync of the main clone, then restart tradedesk-mcp
# - tradedesk-agents (02:30): one agent pipeline step (scripts/nightly_agents.sh)
# - tradedesk-backup (03:30): SQLite backup, 30 days kept
set -euo pipefail
SVC=tradedesk
HOME_DIR="/home/$SVC"
REPO="$HOME_DIR/tradedesk-mcp"
DATA=/var/lib/tradedesk-mcp
UV="$HOME_DIR/.local/bin/uv"

echo "== data dir and environment"
install -d -m 750 -o "$SVC" -g "$SVC" "$DATA" "$DATA/backups"
if [[ ! -f /etc/tradedesk-mcp.env ]]; then
  cat >/etc/tradedesk-mcp.env <<EOF
TRADEDESK_TRANSPORT=streamable-http
TRADEDESK_HOST=127.0.0.1
TRADEDESK_PORT=8000
TRADEDESK_DB_PATH=$DATA/tradedesk.sqlite
TRADEDESK_COLLECTOR_ENABLED=1
TRADEDESK_COLLECT_ATM_TERM_SECONDS=300
TRADEDESK_COLLECT_FUNDING_SECONDS=3600
TRADEDESK_COLLECT_BASIS_SECONDS=300
TRADEDESK_RETENTION_SECONDS=86400
TRADEDESK_RETENTION_ATM_TERM_DAYS=365
TRADEDESK_RETENTION_FUNDING_DAYS=365
TRADEDESK_RETENTION_BASIS_DAYS=365
EOF
fi
chown root:"$SVC" /etc/tradedesk-mcp.env
chmod 640 /etc/tradedesk-mcp.env

echo "== update the clone"
runuser -u "$SVC" -- git -C "$REPO" pull -q --ff-only
runuser -u "$SVC" -- "$UV" --directory "$REPO" sync --quiet --extra dev
test -x "$REPO/.venv/bin/tradedesk-mcp"

echo "== units"
cat >/etc/systemd/system/tradedesk-mcp.service <<EOF
[Unit]
Description=tradedesk-mcp (MCP server + collector, 127.0.0.1:8000)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$SVC
Group=$SVC
WorkingDirectory=$REPO
EnvironmentFile=/etc/tradedesk-mcp.env
ExecStart=$REPO/.venv/bin/tradedesk-mcp
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/tradedesk-update.service <<EOF
[Unit]
Description=tradedesk: pull main and restart the MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/runuser -u $SVC -- git -C $REPO pull -q --ff-only
ExecStart=/usr/sbin/runuser -u $SVC -- $UV --directory $REPO sync --quiet --extra dev
ExecStartPost=/bin/systemctl restart tradedesk-mcp.service
EOF

cat >/etc/systemd/system/tradedesk-agents.service <<EOF
[Unit]
Description=tradedesk: one nightly agent pipeline step
After=network-online.target tradedesk-update.service
Wants=network-online.target

[Service]
Type=oneshot
User=$SVC
Group=$SVC
WorkingDirectory=$REPO
ExecStart=$REPO/scripts/nightly_agents.sh
TimeoutStartSec=4h
EOF

cat >/etc/systemd/system/tradedesk-backup.service <<EOF
[Unit]
Description=tradedesk: SQLite backup

[Service]
Type=oneshot
User=$SVC
Group=$SVC
ExecStart=$REPO/deploy/backup-sqlite.sh $DATA/tradedesk.sqlite $DATA/backups
EOF

timer() {
  cat >"/etc/systemd/system/$1.timer" <<EOF
[Unit]
Description=$1 at $2

[Timer]
OnCalendar=*-*-* $2:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
}
timer tradedesk-update 02:15
timer tradedesk-agents 02:30
timer tradedesk-backup 03:30

systemctl daemon-reload
systemctl enable --now tradedesk-mcp.service tradedesk-update.timer tradedesk-agents.timer tradedesk-backup.timer

echo "== status"
sleep 8
systemctl is-active tradedesk-mcp.service
ss -ltnp | grep ':8000' || echo "port 8000 not listening yet"
systemctl list-timers 'tradedesk-*' --no-pager | head -6
journalctl -u tradedesk-mcp.service -n 8 --no-pager
