#!/bin/bash
# Step 28: the weekly review of the owner's real journal trades in Telegram, Sundays at 20:00 Europe/Kyiv
# (docs/ideas/IDEA-20261018-assistant.md, «Telegram: запис угоди однією командою і тижневий розбір»). The timer itself
# is a user unit kept in git (deploy/user-units/tradedesk-journal-weekly.*) and is installed by tradedesk-update on its
# own; it does nothing until the flag file below exists. This step checks the prerequisites, prints a preview, creates
# the flag and sends one review now so you see it; "off" removes the flag.
# Not a root step: everything runs as tradedesk, no sudo. The flag lives in a tradedesk-owned directory, so any process
# running as tradedesk can create or remove it as well (the same shared switch as steps 22, 25, 26 and 27, Q-20261021-1).
# Read-only: it reads the journal table of the SQLite store (mode=ro) and posts a short text with a button to the
# dashboard's /journal page into the alerts topic through scripts/tg_send.py (token from ~/.config/tradedesk/agents.env;
# step 9 / telegram-setup.sh first). It never places or changes orders.
# Run as tradedesk (its ssh session):
#   bash scripts/server/28-journal-weekly-timer.sh        # turn on
#   bash scripts/server/28-journal-weekly-timer.sh off    # turn off
set -euo pipefail
SVC=tradedesk
[[ "$(id -un)" == "$SVC" ]] || { echo "run this as $SVC (no sudo needed)"; exit 1; }
REPO="/home/$SVC/tradedesk-mcp"
DIR=/var/lib/tradedesk-mcp/assistant
FLAG="$DIR/journal-telegram.on"
DB=/var/lib/tradedesk-mcp/tradedesk.sqlite
UV="/home/$SVC/.local/bin/uv"
SRC="/home/$SVC/.config/tradedesk/agents.env"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ "${1:-}" == off ]]; then
  rm -f "$FLAG"
  echo "weekly journal review: off (the timer stays, it skips while $FLAG is absent)"
  exit 0
fi
[[ -f "$SRC" ]] || { echo "no $SRC: run scripts/server/telegram-setup.sh first"; exit 1; }
[[ -d "$DIR" && -w "$DIR" ]] || { echo "no writable $DIR: run scripts/server/19-assistant-snapshot.sh first"; exit 1; }
git -C "$REPO" pull -q --ff-only
test -f "$REPO/scripts/journal_weekly_tg.py"
test -f "$REPO/deploy/user-units/tradedesk-journal-weekly.timer"
[[ -f "$DB" ]] || echo "warning: no $DB yet: the review will say so"
echo "== preview (sends nothing)"
(cd "$REPO" && "$UV" run --quiet python scripts/journal_weekly_tg.py --print)
touch "$FLAG"
bash "$REPO/scripts/user_units_sync.sh"
echo "== sending one review now"
(cd "$REPO" && "$UV" run --quiet python scripts/journal_weekly_tg.py) || echo "send failed: see the message above"
systemctl --user list-timers tradedesk-journal-weekly.timer --no-pager
