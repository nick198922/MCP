# Sourced by the agents scripts: the Claude plan's usage, the same figures as /usage, read with this user's Claude
# login. The token is passed to curl through stdin; it is never printed or logged. The figures are for the whole
# account (the agents, the doctor, the Telegram chat and the owner's own sessions together).

# The usage JSON, or nothing when it cannot be read. The endpoint rate-limits (HTTP 429 when polled every minute, seen
# 2026-10-17), so the last good answer is kept in $LOG_DIR/usage.json: reused without a call while under 2 minutes old,
# and as the fallback while under an hour old, so the 97 % pause and the 80 % budget rule never fail open on a 429.
usage_json() {
  local token fresh cache="${LOG_DIR:-$HOME/.local/state/tradedesk-agents}/usage.json" age=999999
  [[ -s "$cache" ]] && age=$(($(date +%s) - $(date -r "$cache" +%s)))
  if ((age < 120)); then
    cat "$cache"
    return 0
  fi
  token="$(jq -r '.claudeAiOauth.accessToken // empty' "$HOME/.claude/.credentials.json" 2>/dev/null)"
  if [[ -n "$token" ]]; then
    fresh="$(printf 'header = "Authorization: Bearer %s"\n' "$token" | curl -s -m 15 -K - https://api.anthropic.com/api/oauth/usage \
      -H "anthropic-beta: oauth-2025-04-20" || true)"
    if jq -e '.seven_day.utilization != null and .five_hour.utilization != null' <<<"$fresh" >/dev/null 2>&1; then
      printf '%s\n' "$fresh" >"$cache.tmp" && mv "$cache.tmp" "$cache"
      printf '%s\n' "$fresh"
      return 0
    fi
  fi
  if ((age < 3600)); then cat "$cache"; fi
  return 0
}

# "<5-hour %> <7-day %>", or nothing when unknown.
usage_now() {
  usage_json | jq -r 'if (.five_hour.utilization? != null and .seven_day.utilization? != null)
    then "\(.five_hour.utilization) \(.seven_day.utilization)" else empty end' 2>/dev/null || true
}

# usage_change "<before>" "<after>": "5 год +12% → 40%, тиждень +3% → 21%" (a window that reset meanwhile shows its
# new figure only).
usage_change() {
  [[ -z "$1" || -z "$2" ]] && return 0
  awk -v a="$1" -v b="$2" 'BEGIN {
    split(a, x, " "); split(b, y, " ")
    for (i = 1; i <= 2; i++) {
      d = y[i] - x[i]
      s[i] = (d < 0 ? "оновився, " : sprintf("%+.0f%% → ", d)) sprintf("%.0f%%", y[i])
    }
    printf "5 год %s, тиждень %s", s[1], s[2]
  }'
}
