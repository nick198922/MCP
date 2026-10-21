#!/bin/bash
# Step 10: talk to Claude from the Telegram topic "💬 Claude" (scripts/tg_bridge.py). Run through nick:
#   ssh nick@SERVER 'sudo bash -s' < scripts/server/10-tg-bridge.sh
# Only the group's creator is heard. Claude works in its own clone ~/tradedesk-chat with the owner's server
# permissions (no sudo, no force push, no reading keys) and delivers changes as chat/* branches; main is protected.
set -euo pipefail
SVC=tradedesk
H="/home/$SVC"
REPO="$H/tradedesk-mcp"
CHAT="$H/tradedesk-chat"
as() { runuser -u "$SVC" -- "$@"; }

as git -C "$REPO" pull -q --ff-only
test -f "$REPO/scripts/tg_bridge.py"
if [[ ! -d "$CHAT/.git" ]]; then
  as git clone -q "$(as git -C "$REPO" remote get-url origin)" "$CHAT"
fi
as git -C "$CHAT" config user.name "Mykola Khanetskyi (server chat)"
as git -C "$CHAT" config user.email "n.hanetskiy@gmail.com"
as mkdir -p "$CHAT/.claude"
as tee "$CHAT/.claude/settings.local.json" >/dev/null <<'JSON'
{
  "permissions": {
    "defaultMode": "acceptEdits",
    "allow": ["Bash", "WebFetch", "WebSearch", "mcp__tradedesk"],
    "deny": [
      "Bash(sudo:*)",
      "Bash(git push --force:*)",
      "Bash(git push -f:*)",
      "Bash(git push origin main:*)",
      "Bash(git reset --hard:*)",
      "Bash(git config:*)",
      "Bash(rm -rf /:*)",
      "Bash(rm -rf ~:*)",
      "Bash(rm -rf /home:*)",
      "Read(~/.ssh/**)",
      "Read(~/.claude/.credentials.json)",
      "Read(~/.config/tradedesk/**)",
      "Read(//etc/tradedesk-mcp.env)"
    ]
  }
}
JSON
as chmod 600 "$CHAT/.claude/settings.local.json"
# The project's own MCP (localhost, read-only tools) for market pictures and trade plans (plan_add) from the chat.
as bash -c "cd '$CHAT' && ~/.local/bin/claude mcp remove --scope local tradedesk >/dev/null 2>&1; ~/.local/bin/claude mcp add --scope local --transport http tradedesk http://127.0.0.1:8000/mcp >/dev/null"
as bash -c "cd '$CHAT' && ~/.local/bin/uv sync --quiet --extra dev"

# The "💬 Claude" topic and the owner's Telegram id (the group's creator).
as python3 "$REPO/scripts/telegram_topics.py"
grep -q '^TELEGRAM_OWNER_ID=.' "$H/.config/tradedesk/agents.env" || { echo "owner id not found: is the group's creator visible to the bot?"; exit 1; }

cat >/etc/systemd/system/tradedesk-tgbridge.service <<UNIT
[Unit]
Description=tradedesk: the owner talks to Claude from the Telegram topic "Claude"
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC
Group=$SVC
WorkingDirectory=$REPO
Environment=TRADEDESK_CHAT_CLONE=$CHAT
ExecStart=/usr/bin/python3 $REPO/scripts/tg_bridge.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now tradedesk-tgbridge.service
sleep 3
systemctl is-active tradedesk-tgbridge.service
tail -3 "$H/.local/state/tradedesk-agents/tg-bridge.log" 2>/dev/null || true
