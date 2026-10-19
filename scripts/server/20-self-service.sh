#!/bin/bash
# Step 20: let merged pull requests reach the server without the owner (once; then no more steps like 17-19).
#   - tradedesk-update runs hourly instead of 02:15 and restarts tradedesk-mcp only when main moved;
#   - after the pull it syncs the user timers from git (scripts/user_units_sync.sh, as tradedesk);
#   - linger for tradedesk, so its user timers run without a login session.
# Feature switches of read-only jobs come from deploy/features.env in git (src/tradedesk/features.py, allow-list);
# /etc/tradedesk-mcp.env keeps priority. Nothing here gives tradedesk root rights.
# Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/20-self-service.sh
set -euo pipefail
SVC=tradedesk
REPO="/home/$SVC/tradedesk-mcp"
UV="/home/$SVC/.local/bin/uv"
runuser -u "$SVC" -- git -C "$REPO" pull -q --ff-only
test -f "$REPO/src/tradedesk/features.py" && test -f "$REPO/scripts/user_units_sync.sh"

loginctl enable-linger "$SVC"

# The update itself: root only restarts; every repository command runs as tradedesk.
cat >/usr/local/sbin/tradedesk-update <<EOF
#!/bin/bash
set -euo pipefail
as() { /usr/sbin/runuser -u $SVC -- "\$@"; }
old="\$(as git -C $REPO rev-parse HEAD)"
as git -C $REPO pull -q --ff-only
new="\$(as git -C $REPO rev-parse HEAD)"
as env XDG_RUNTIME_DIR=/run/user/\$(id -u $SVC) bash $REPO/scripts/user_units_sync.sh || echo "user units sync failed"
if [[ "\$old" == "\$new" && "\${1:-}" != --force ]]; then echo "main unchanged (\${new:0:7})"; exit 0; fi
as $UV --directory $REPO sync --quiet --extra dev
systemctl restart tradedesk-mcp.service
echo "updated \${old:0:7} -> \${new:0:7}, tradedesk-mcp restarted"
EOF
chmod 755 /usr/local/sbin/tradedesk-update

install -d /etc/systemd/system/tradedesk-update.service.d /etc/systemd/system/tradedesk-update.timer.d
cat >/etc/systemd/system/tradedesk-update.service.d/20-self-service.conf <<EOF
[Service]
ExecStart=
ExecStartPost=
ExecStart=/usr/local/sbin/tradedesk-update
EOF
cat >/etc/systemd/system/tradedesk-update.timer.d/20-self-service.conf <<EOF
[Timer]
OnCalendar=
OnCalendar=*-*-* *:20:00
EOF
systemctl daemon-reload
systemctl restart tradedesk-update.timer
/usr/local/sbin/tradedesk-update --force
systemctl list-timers tradedesk-update.timer --no-pager | head -3
loginctl show-user "$SVC" -p Linger
