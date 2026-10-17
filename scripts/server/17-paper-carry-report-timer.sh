#!/bin/bash
# Step 17: weekly paper-carry report (scripts/paper_carry_report.py) as a systemd timer, Sundays 20:00 Europe/Kyiv, user
# tradedesk. The report reads a snapshot copy of the collector DB (no network, never writes to the DB) and writes
# REPORT.md OUTSIDE the git checkout ($OUT; the checkout is updated by git pull --ff-only). It does NOT enable the engine (TRADEDESK_PAPER_CARRY_ENABLED stays as it is).
# Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/17-paper-carry-report-timer.sh
set -euo pipefail
SVC=tradedesk
REPO="/home/$SVC/tradedesk-mcp"
OUT_DIR=/var/lib/tradedesk-mcp/paper
OUT=$OUT_DIR/REPORT.md
DB=/var/lib/tradedesk-mcp/tradedesk.sqlite
UV="/home/$SVC/.local/bin/uv"
runuser -u "$SVC" -- git -C "$REPO" pull -q --ff-only
test -f "$REPO/scripts/paper_carry_report.py"
install -d -o "$SVC" -g "$SVC" "$OUT_DIR"
cat >/etc/systemd/system/tradedesk-paper-report.service <<UNIT
[Unit]
Description=tradedesk: weekly paper carry report ($OUT)

[Service]
Type=oneshot
User=$SVC
Group=$SVC
WorkingDirectory=$REPO
ExecStart=$UV run python scripts/paper_carry_report.py --db $DB --out $OUT
TimeoutStartSec=10min
NoNewPrivileges=true
UNIT
cat >/etc/systemd/system/tradedesk-paper-report.timer <<UNIT
[Unit]
Description=tradedesk-paper-report on Sundays 20:00 Europe/Kyiv

[Timer]
OnCalendar=Sun *-*-* 20:00:00 Europe/Kyiv
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now tradedesk-paper-report.timer
systemctl list-timers tradedesk-paper-report.timer --no-pager
