#!/bin/bash
# Step 30: the weekly counters-only report of the assistant_paper forward journal (scripts/assistant_paper_report.py;
# item 284 of docs/ideas/IDEA-20261102-standing-improvements.md), Sundays at 20:15 Europe/Kyiv. Trades, unsettled,
# stretches without a standing trade, the job's last daily decision; NO P&L. The timer itself is a user unit kept in git
# (deploy/user-units/tradedesk-assistant-paper-report.*) and is installed by tradedesk-update on its own; it does nothing
# until the flag file below exists. This step checks the prerequisites, prints a preview, creates the flag and sends one
# report now so you see it; "off" removes the flag.
# Not a root step: everything runs as tradedesk, no sudo (step 17's pattern needed root for /etc/systemd/system; the user
# units do not). The flag lives in a tradedesk-owned directory, so any process running as tradedesk can create or remove
# it as well (the same shared switch as steps 22 and 25-29, Q-20261021-1).
# Read-only: it opens the SQLite store with mode=ro, writes the report to $OUT (outside the git checkout) and posts it to
# the agents topic through scripts/tg_send.py (token from ~/.config/tradedesk/agents.env; step 9 / telegram-setup.sh
# first). No exchange keys, it never places or changes orders.
# Run as tradedesk (its ssh session):
#   bash scripts/server/30-assistant-paper-report-timer.sh        # turn on
#   bash scripts/server/30-assistant-paper-report-timer.sh off    # turn off
set -euo pipefail
SVC=tradedesk
[[ "$(id -un)" == "$SVC" ]] || { echo "run this as $SVC (no sudo needed)"; exit 1; }
REPO="/home/$SVC/tradedesk-mcp"
DIR=/var/lib/tradedesk-mcp/assistant
FLAG="$DIR/assistant-paper-report.on"
DB=/var/lib/tradedesk-mcp/tradedesk.sqlite
UV="/home/$SVC/.local/bin/uv"
SRC="/home/$SVC/.config/tradedesk/agents.env"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ "${1:-}" == off ]]; then
  rm -f "$FLAG"
  echo "weekly assistant_paper report: off (the timer stays, it skips while $FLAG is absent)"
  exit 0
fi
[[ -f "$SRC" ]] || { echo "no $SRC: run scripts/server/telegram-setup.sh first"; exit 1; }
[[ -d "$DIR" && -w "$DIR" ]] || { echo "no writable $DIR: run scripts/server/19-assistant-snapshot.sh first"; exit 1; }
git -C "$REPO" pull -q --ff-only
test -f "$REPO/scripts/assistant_paper_report.py"
test -f "$REPO/deploy/user-units/tradedesk-assistant-paper-report.timer"
[[ -f "$DB" ]] || echo "warning: no $DB yet: the report will say so"
echo "== preview (writes and sends nothing)"
(cd "$REPO" && "$UV" run --quiet python scripts/assistant_paper_report.py --dry-run) || true
touch "$FLAG"
bash "$REPO/scripts/user_units_sync.sh"
echo "== writing and sending one report now"
(cd "$REPO" && "$UV" run --quiet python scripts/assistant_paper_report.py --telegram) || echo "report or send failed: see the message above"
systemctl --user list-timers tradedesk-assistant-paper-report.timer --no-pager
