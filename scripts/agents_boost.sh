#!/bin/bash
# The owner's boost: for N hours the agents ignore the weekly pace and the owner's reserve (scripts/agents_loop.sh);
# the MAX_USAGE brake stays. Run as tradedesk, no sudo; the loop reads the file before every step.
#   bash scripts/agents_boost.sh 48     # on for 48 hours
#   bash scripts/agents_boost.sh off    # off now
#   bash scripts/agents_boost.sh        # status
set -euo pipefail
LOG_DIR="${TRADEDESK_AGENTS_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tradedesk-agents}"
F="$LOG_DIR/boost-until"
case "${1:-}" in
  "")
    u="$(head -c 20 "$F" 2>/dev/null | tr -cd '0-9' || true)"
    if [[ -n "$u" ]] && ((u > $(date +%s))); then echo "boost on until $(date -d "@$u" '+%d.%m %H:%M')"; else echo "boost off"; fi
    ;;
  off) rm -f "$F" && echo "boost off" ;;
  *[!0-9]*) echo "usage: $0 [HOURS|off]" >&2 && exit 2 ;;
  *)
    (($1 >= 1 && $1 <= 168)) || { echo "hours: 1..168" >&2 && exit 2; }
    mkdir -p "$LOG_DIR"
    echo $(($(date +%s) + $1 * 3600)) >"$F"
    echo "boost on until $(date -d "@$(cat "$F")" '+%d.%m %H:%M')"
    ;;
esac
