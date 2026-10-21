#!/bin/bash
# Step 22: the assistant's morning brief in Telegram, daily at 08:00 Europe/Kyiv (docs/ideas/IDEA-20261018-assistant.md).
# The timer itself is a user unit kept in git (deploy/user-units/tradedesk-assistant-brief.*) and is installed by
# tradedesk-update on its own; it does nothing until the flag file below exists. This step checks the prerequisites,
# prints a preview, creates the flag and sends one brief now so you see it; "off" removes the flag.
# Not a root step: everything runs as tradedesk, no sudo. The flag lives in a tradedesk-owned directory, so any process
# running as tradedesk (the agents, the collector, a Claude session) can create or remove it as well: this is a
# convenience switch, not an owner-only control (docs/QUESTIONS.md, Q-20261021-1 asks the owner whether that is fine).
# Read-only: it reads the snapshot JSON (step 19) and posts text (and a PNG when matplotlib is in the venv) into the
# alerts topic through scripts/tg_send.py (token from ~/.config/tradedesk/agents.env; step 9 / telegram-setup.sh first).
# Run as tradedesk (its ssh session):
#   bash scripts/server/22-assistant-brief-timer.sh        # turn on
#   bash scripts/server/22-assistant-brief-timer.sh off    # turn off
set -euo pipefail
SVC=tradedesk
[[ "$(id -un)" == "$SVC" ]] || { echo "run this as $SVC (no sudo needed)"; exit 1; }
REPO="/home/$SVC/tradedesk-mcp"
DIR=/var/lib/tradedesk-mcp/assistant
FLAG="$DIR/brief-telegram.on"
UV="/home/$SVC/.local/bin/uv"
SRC="/home/$SVC/.config/tradedesk/agents.env"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ "${1:-}" == off ]]; then
  rm -f "$FLAG"
  echo "morning brief: off (the timer stays, it skips while $FLAG is absent)"
  exit 0
fi
[[ -f "$SRC" ]] || { echo "no $SRC: run scripts/server/telegram-setup.sh first"; exit 1; }
[[ -d "$DIR" && -w "$DIR" ]] || { echo "no writable $DIR: run scripts/server/19-assistant-snapshot.sh first"; exit 1; }
git -C "$REPO" pull -q --ff-only
test -f "$REPO/scripts/assistant_brief_tg.py"
test -f "$REPO/deploy/user-units/tradedesk-assistant-brief.timer"
[[ -f "$DIR/snapshot.json" ]] || echo "warning: no $DIR/snapshot.json yet (step 19): the brief will say so"
echo "== preview (sends nothing)"
(cd "$REPO" && "$UV" run --quiet python scripts/assistant_brief_tg.py --print)
touch "$FLAG"
bash "$REPO/scripts/user_units_sync.sh"
echo "== sending one brief now"
(cd "$REPO" && "$UV" run --quiet python scripts/assistant_brief_tg.py) || echo "send failed: see the message above"
systemctl --user list-timers tradedesk-assistant-brief.timer --no-pager
