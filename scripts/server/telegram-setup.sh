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
tg() { printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$token" "$1" | curl -s -m 20 -K -; }

me="$(tg getMe)" || true
if [[ "$(jq -r '.ok' <<<"$me" 2>/dev/null)" != "true" ]]; then
  echo "Telegram did not accept the token: $(jq -r '.description // empty' <<<"$me" 2>/dev/null || echo "no answer from api.telegram.org")"
  exit 1
fi
bot="$(jq -r '.result.username' <<<"$me")"
echo "bot: @$bot"

updates="$(tg getUpdates)" || true
if [[ "$(jq -r '.ok' <<<"$updates" 2>/dev/null)" != "true" ]]; then
  echo "getUpdates failed: $(jq -r '.description // empty' <<<"$updates" 2>/dev/null)"
  exit 1
fi
echo "chats the bot has seen:"
jq -r '[ .result[] | (.message // .edited_message // .channel_post // .my_chat_member // empty) | .chat ]
  | unique_by(.id)[] | "  \(.type)  \(.title // .username // .first_name // "")  (\(.id))"' <<<"$updates"
chat="$(jq -r '
  [ .result[] | (.message // .edited_message // .channel_post // .my_chat_member // empty)
    | (.chat | select(.type == "group" or .type == "supergroup" or .type == "channel") | .id), (.migrate_to_chat_id // empty) ]
  | last // empty' <<<"$updates")"
if [[ -z "$chat" ]]; then
  echo "No group or channel above. Add @$bot to the group as a member (a channel: as an admin),"
  echo "then send /start@$bot in the group and run this again."
  exit 1
fi
echo "using chat id: $chat"

printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' "$token" "$chat" | ssh tradedesk@"$SERVER" \
  'umask 077 && mkdir -p ~/.config/tradedesk && cat > ~/.config/tradedesk/agents.env && chmod 600 ~/.config/tradedesk/agents.env'

ssh tradedesk@"$SERVER" 'bash -s' <<'EOF'
. ~/.config/tradedesk/agents.env
code="$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" | curl -s -m 20 -o /dev/null -w '%{http_code}' -K - \
  --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" --data-urlencode "text=tradedesk: сповіщення агентів підключено ✅")"
echo "test message from the server: HTTP $code (200 = check the group)"
EOF
