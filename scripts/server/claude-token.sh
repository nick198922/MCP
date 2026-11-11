#!/bin/bash
# Run as tradedesk in an ssh session (not through `!` in Claude: it needs the browser and a pasted code):
#   bash ~/tradedesk-mcp/scripts/server/claude-token.sh
# Makes a one-year backup token with `claude setup-token` and saves it to ~/.config/tradedesk/claude-token (0600).
# scripts/claude_run.sh uses it only while the ordinary login has ended, so the Telegram bot and the agents keep working
# until you log in again. The token is never printed by this script, logged or put into git.
set -euo pipefail
CLAUDE="${TRADEDESK_CLAUDE_REAL:-$HOME/.local/bin/claude}"
FILE="${TRADEDESK_CLAUDE_TOKEN_FILE:-$HOME/.config/tradedesk/claude-token}"

echo "1) Зараз відкриється вхід Claude: відкрий посилання в браузері, підтвердь і встав код сюди."
echo "   У кінці claude покаже токен (sk-ant-oat01-…): скопіюй його."
"$CLAUDE" setup-token
echo
read -r -s -p "2) Встав токен і натисни Enter (на екрані його не буде видно): " token
echo
token="$(printf '%s' "$token" | tr -d '[:space:]')"
if [[ "$token" != sk-ant-* ]]; then
  echo "Це не схоже на токен (має починатися з sk-ant-). Нічого не збережено."
  exit 1
fi
echo "3) Перевіряю токен…"
if ! out="$(cd /tmp && CLAUDE_CODE_OAUTH_TOKEN="$token" timeout 120 "$CLAUDE" -p "Reply with the single word: ok" --model haiku 2>&1)"; then
  echo "Токен не працює: ${out:0:300}"
  echo "Нічого не збережено."
  exit 1
fi
mkdir -p "$(dirname "$FILE")"
umask 077
printf '%s\n' "$token" >"$FILE.tmp"
chmod 600 "$FILE.tmp"
mv "$FILE.tmp" "$FILE"
echo "Готово: запасний токен збережено ($FILE, 0600)."
python3 -I "$(dirname "$0")/../claude_auth.py" status
