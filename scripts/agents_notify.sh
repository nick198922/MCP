# Sourced by the agents scripts. notify "<text>" [--silent] [--button "Label|https://url"]... sends the text (light
# markdown) to the owner's Telegram group through scripts/tg_send.py, which reads TELEGRAM_BOT_TOKEN and
# TELEGRAM_CHAT_ID from ~/.config/tradedesk/agents.env (0600; see scripts/server/telegram-setup.sh).
# On a Mac without those settings it shows a desktop notification instead.
[[ -f "$HOME/.config/tradedesk/agents.env" ]] && . "$HOME/.config/tradedesk/agents.env"
_TG_SEND="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tg_send.py"

notify() {
  local text="$1"
  shift
  if [[ -n "${TRADEDESK_AGENTS_NO_NOTIFY:-}" ]]; then
    echo "NOTIFY: $text${*:+ [$*]}"
    return 0
  fi
  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    printf '%s' "$text" | python3 "$_TG_SEND" "$@" || true
  elif [[ "$(uname)" == "Darwin" ]]; then
    osascript -e "display notification \"$(printf '%s' "$text" | head -c 200 | tr '"' "'")\" with title \"tradedesk: агенти\"" || true
  fi
}
