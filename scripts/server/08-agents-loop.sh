#!/bin/bash
# Step 8: agents work continuously (scripts/agents_loop.sh) instead of once a night. Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/08-agents-loop.sh
set -euo pipefail
SVC=tradedesk
REPO="/home/$SVC/tradedesk-mcp"

runuser -u "$SVC" -- git -C "$REPO" pull -q --ff-only
test -x "$REPO/scripts/agents_loop.sh"

cat >/etc/systemd/system/tradedesk-agents-loop.service <<EOF
[Unit]
Description=tradedesk: continuous agent pipeline (one step at a time, pauses at the usage limit)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC
Group=$SVC
WorkingDirectory=$REPO
Environment=TRADEDESK_AGENTS_MAX_USAGE=95
Environment=TRADEDESK_AGENTS_MAX_OPEN=2
ExecStart=$REPO/scripts/agents_loop.sh
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl disable --now tradedesk-agents.timer 2>/dev/null || true
systemctl enable --now tradedesk-agents-loop.service
sleep 5
systemctl is-active tradedesk-agents-loop.service
journalctl -u tradedesk-agents-loop.service -n 5 --no-pager
