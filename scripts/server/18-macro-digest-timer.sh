#!/bin/bash
# Step 18: macro digest to Telegram (scripts/macro_digest.py) as two systemd timers, user tradedesk:
#   - tradedesk-macro-week:   weekly overview, Sundays 20:00 Europe/Kyiv;
#   - tradedesk-macro-remind: reminders for events starting within 60 min, every 15 minutes.
# Read-only: it fetches the ForexFactory calendar and sends text into the alerts topic through scripts/tg_send.py
# (token and chat id from ~/.config/tradedesk/agents.env, as for the agents; step 9 / telegram-setup.sh must be done first).
# Saves weekly feed copies and the sent-reminder list under $DATA_DIR (outside the git checkout).
# Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/18-macro-digest-timer.sh
set -euo pipefail
SVC=tradedesk
REPO="/home/$SVC/tradedesk-mcp"
DATA_DIR=/var/lib/tradedesk-mcp/macro
UV="/home/$SVC/.local/bin/uv"
SRC="/home/$SVC/.config/tradedesk/agents.env"
[[ -f "$SRC" ]] || { echo "no $SRC: run scripts/server/telegram-setup.sh first"; exit 1; }
runuser -u "$SVC" -- git -C "$REPO" pull -q --ff-only
test -f "$REPO/scripts/macro_digest.py"
install -d -o "$SVC" -g "$SVC" "$DATA_DIR"
unit() { # name, description, command, calendar, persistent
cat >"/etc/systemd/system/tradedesk-macro-$1.service" <<UNIT
[Unit]
Description=tradedesk: macro digest ($2)

[Service]
Type=oneshot
User=$SVC
Group=$SVC
WorkingDirectory=$REPO
ExecStart=$UV run python scripts/macro_digest.py $3 --data-dir $DATA_DIR
TimeoutStartSec=3min
NoNewPrivileges=true
UNIT
cat >"/etc/systemd/system/tradedesk-macro-$1.timer" <<UNIT
[Unit]
Description=tradedesk-macro-$1 ($4)

[Timer]
OnCalendar=$4
Persistent=$5

[Install]
WantedBy=timers.target
UNIT
}
unit week "weekly overview" week "Sun *-*-* 20:00:00 Europe/Kyiv" true
unit remind "event reminders" remind "*-*-* *:0/15:00 UTC" false
systemctl daemon-reload
systemctl enable --now tradedesk-macro-week.timer tradedesk-macro-remind.timer
systemctl list-timers 'tradedesk-macro-*' --no-pager
echo "== dry run (prints, sends nothing)"
runuser -u "$SVC" -- bash -c "cd $REPO && $UV run python scripts/macro_digest.py week --dry-run --data-dir $DATA_DIR" || true
