#!/bin/bash
# Install (or reinstall) the launchd job that runs scripts/nightly_agents.sh every night at 02:30.
# Remove it with: launchctl bootout gui/$(id -u)/com.tradedesk.agents && rm ~/Library/LaunchAgents/com.tradedesk.agents.plist
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.tradedesk.agents"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/tradedesk-agents"
mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/scripts/nightly_agents.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>$LOG_DIR/launchd.out</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/launchd.err</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $PLIST; next run at 02:30 (a sleeping Mac runs it on wake)"
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state|path" | head -3
