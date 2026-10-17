#!/bin/bash
# Step 15: the project dashboard (scripts/dashboard.py) as a service on 127.0.0.1:8090, run as tradedesk. It only reads
# (agents' state and history, idea cards, plan usage, SQLite read-only, /proc) and re-execs itself when the repo
# brings a new version. Not reachable from outside: open it from the Mac with scripts/dashboard-open.sh (SSH tunnel).
# Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/15-dashboard.sh
set -euo pipefail
SVC=tradedesk
REPO="/home/$SVC/tradedesk-mcp"
runuser -u "$SVC" -- git -C "$REPO" pull -q --ff-only
test -f "$REPO/scripts/dashboard.py"
cat >/etc/systemd/system/tradedesk-dashboard.service <<UNIT
[Unit]
Description=tradedesk: project dashboard on 127.0.0.1:8090 (read-only)
After=network-online.target

[Service]
User=$SVC
WorkingDirectory=$REPO
Environment=TRADEDESK_AGENTS_MAX_USAGE=97
ExecStart=/usr/bin/python3 -I $REPO/scripts/dashboard.py serve --port 8090
Restart=always
RestartSec=10
MemoryMax=300M
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now tradedesk-dashboard.service
sleep 8
systemctl is-active tradedesk-dashboard.service
curl -s -o /dev/null -w 'dashboard HTTP %{http_code}\n' http://127.0.0.1:8090/
