#!/bin/bash
# The Claude CLI for the server's headless jobs (TRADEDESK_CLAUDE_BIN of the Telegram bridge, the agents, the doctor, the
# audit, channel_watch and trader_review). While the interactive login (`/login`, about 30 days) is valid, it runs the
# CLI as it is, so the plan usage figures (they need the login) keep working. When the login has ended and a backup
# token from `claude setup-token` exists (scripts/server/claude-token.sh), the CLI gets it as CLAUDE_CODE_OAUTH_TOKEN, so
# the jobs keep running until the owner logs in again; scripts/claude_auth.py check tells the owner either way.
# exec keeps the pid, the signals and the streamed output of the CLI exactly as without the wrapper.
REAL="${TRADEDESK_CLAUDE_REAL:-$HOME/.local/bin/claude}"
[[ -x "$REAL" ]] || REAL="$(command -v claude || echo claude)"
TOKEN_FILE="${TRADEDESK_CLAUDE_TOKEN_FILE:-$HOME/.config/tradedesk/claude-token}"
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -s "$TOKEN_FILE" ]] &&
  ! TRADEDESK_CLAUDE_REAL="$REAL" python3 -I "$(dirname "${BASH_SOURCE[0]}")/claude_auth.py" login-ok; then
  CLAUDE_CODE_OAUTH_TOKEN="$(head -1 "$TOKEN_FILE" | tr -d '[:space:]')"
  export CLAUDE_CODE_OAUTH_TOKEN
fi
exec "$REAL" "$@"
