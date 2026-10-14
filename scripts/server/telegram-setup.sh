#!/bin/bash
# From the Mac, in the Terminal app (it asks for the bot token, so it does not work through Claude's "!"):
#   bash ~/MCP/tradedesk-mcp/scripts/server/telegram-setup.sh
# Before: create the bot with @BotFather, add it to the group, send any message in the group.
# The token goes straight into the server's ~/.config/tradedesk/agents.env (0600): never into git, logs or chat.
# The bot only sends the step summaries; nothing reads messages from it.
set -euo pipefail
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"

read -rsp "Bot token from @BotFather (hidden while you type): " token
echo
if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
  echo "This does not look like a bot token (digits:letters). Run it again in the Terminal app and paste the token."
  exit 1
fi

# The token is passed to curl through stdin, not the command line.
chat="$(printf 'url = "https://api.telegram.org/bot%s/getUpdates"\n' "$token" | curl -s -m 20 -K - | jq -r '
  [ .result[]? | (.message // .my_chat_member // empty)
    | (.chat | select(.type == "group" or .type == "supergroup") | .id), (.migrate_to_chat_id // empty) ]
  | last // empty')"
if [[ -z "$chat" ]]; then
  echo "The bot sees no group yet: add it to the group, send any message there (e.g. /start), then run this again."
  exit 1
fi
echo "group chat id: $chat"

printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$token" "$chat" | ssh tradedesk@"$SERVER" \
  'umask 077 && mkdir -p ~/.config/tradedesk && cat > ~/.config/tradedesk/agents.env && chmod 600 ~/.config/tradedesk/agents.env'

ssh tradedesk@"$SERVER" 'bash -s' <<'EOF'
. ~/.config/tradedesk/agents.env
code="$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" | curl -s -m 20 -o /dev/null -w '%{http_code}' -K - \
  --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" --data-urlencode "text=tradedesk: сповіщення агентів підключено ✅")"
echo "test message from the server: HTTP $code (200 = check the group)"
EOF
