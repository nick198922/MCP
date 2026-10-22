#!/bin/bash
# Step 25: the assistant's Sunday radar in Telegram, Sundays at 19:30 Europe/Kyiv (docs/ideas/IDEA-20261018-assistant.md).
# The timer itself is a user unit kept in git (deploy/user-units/tradedesk-radar-weekly.*) and is installed by
# tradedesk-update on its own; it does nothing until the flag file below exists. This step checks the prerequisites,
# prints a preview, creates the flag and sends one radar now so you see it; "off" removes the flag.
# Not a root step: everything runs as tradedesk, no sudo. The flag lives in a tradedesk-owned directory, so any process
# running as tradedesk can create or remove it as well (the same shared switch as step 22, Q-20261021-1).
# Read-only: it reads the snapshot JSON (step 19, its `radar` section) and posts a short text with buttons to the
# dashboard's /radar page into the alerts topic through scripts/tg_send.py (token from ~/.config/tradedesk/agents.env;
# step 9 / telegram-setup.sh first). No PNG: the charts are SVG on the radar page (owner, 22.10).
# Run as tradedesk (its ssh session):
#   bash scripts/server/25-radar-weekly-timer.sh        # turn on
#   bash scripts/server/25-radar-weekly-timer.sh off    # turn off
set -euo pipefail
SVC=tradedesk
[[ "$(id -un)" == "$SVC" ]] || { echo "run this as $SVC (no sudo needed)"; exit 1; }
REPO="/home/$SVC/tradedesk-mcp"
DIR=/var/lib/tradedesk-mcp/assistant
FLAG="$DIR/radar-telegram.on"
UV="/home/$SVC/.local/bin/uv"
SRC="/home/$SVC/.config/tradedesk/agents.env"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ "${1:-}" == off ]]; then
  rm -f "$FLAG"
  echo "Sunday radar: off (the timer stays, it skips while $FLAG is absent)"
  exit 0
fi
[[ -f "$SRC" ]] || { echo "no $SRC: run scripts/server/telegram-setup.sh first"; exit 1; }
[[ -d "$DIR" && -w "$DIR" ]] || { echo "no writable $DIR: run scripts/server/19-assistant-snapshot.sh first"; exit 1; }
git -C "$REPO" pull -q --ff-only
test -f "$REPO/scripts/radar_weekly_tg.py"
test -f "$REPO/deploy/user-units/tradedesk-radar-weekly.timer"
[[ -f "$DIR/snapshot.json" ]] || echo "warning: no $DIR/snapshot.json yet (step 19): the radar will say so"
echo "== preview (sends nothing)"
(cd "$REPO" && "$UV" run --quiet python scripts/radar_weekly_tg.py --print)
touch "$FLAG"
bash "$REPO/scripts/user_units_sync.sh"
echo "== sending one radar now"
(cd "$REPO" && "$UV" run --quiet python scripts/radar_weekly_tg.py) || echo "send failed: see the message above"
systemctl --user list-timers tradedesk-radar-weekly.timer --no-pager
