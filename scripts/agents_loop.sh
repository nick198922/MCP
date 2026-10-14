#!/bin/bash
# Continuous agent pipeline: one step at a time (scripts/nightly_agents.sh), with three brakes:
#  - plan usage: if the 5-hour or 7-day utilization is at or above TRADEDESK_AGENTS_MAX_USAGE (%), sleep until it resets
#  - review backlog: if TRADEDESK_AGENTS_MAX_OPEN agent branches wait for the owner, sleep and check again
#  - idle: if a step delivered nothing, sleep longer before the next one
# Runs as a systemd service (scripts/server/08-agents-loop.sh). Restart the service after changing this file.
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MAX_USAGE="${TRADEDESK_AGENTS_MAX_USAGE:-95}"
MAX_OPEN="${TRADEDESK_AGENTS_MAX_OPEN:-2}"
WAIT_REVIEW="${TRADEDESK_AGENTS_REVIEW_WAIT_SECONDS:-1800}"
WAIT_IDLE="${TRADEDESK_AGENTS_IDLE_WAIT_SECONDS:-7200}"
GAP="${TRADEDESK_AGENTS_GAP_SECONDS:-300}"

log() { echo "$(date '+%F %T') $*"; }

# Prints "<seconds> <reason>" when usage is at or above MAX_USAGE, nothing when below or unknown.
# Reads the same plan-usage figures as /usage; the token is never printed or logged.
usage_wait() {
  local token resp
  token="$(jq -r '.claudeAiOauth.accessToken // empty' "$HOME/.claude/.credentials.json" 2>/dev/null)"
  [[ -z "$token" ]] && return 0
  resp="$(curl -s -m 15 https://api.anthropic.com/api/oauth/usage \
    -H "Authorization: Bearer $token" -H "anthropic-beta: oauth-2025-04-20")" || return 0
  echo "$resp" | jq -r --argjson max "$MAX_USAGE" '
    [ to_entries[]
      | select(.key == "five_hour" or .key == "seven_day")
      | select((.value.utilization // -1) >= $max)
      | {k: .key, u: .value.utilization,
         r: (.value.resets_at | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601)} ]
    | if length == 0 then empty
      else (max_by(.r)) as $m | "\(($m.r - now | floor) + 300) \($m.k)=\($m.u)%"
      end' 2>/dev/null || true
}

log "agents loop started (max usage ${MAX_USAGE}%, max open branches ${MAX_OPEN})"
while true; do
  git -C "$REPO" fetch -q origin || log "fetch failed"
  open="$(git -C "$REPO" branch -r --no-merged origin/main --list 'origin/agent/*' | wc -l)"
  if (( open >= MAX_OPEN )); then
    log "waiting ${WAIT_REVIEW}s: $open agent branches await the owner's review"
    sleep "$WAIT_REVIEW"
    continue
  fi
  w="$(usage_wait)"
  if [[ -n "$w" ]]; then
    secs="${w%% *}"
    (( secs < 60 )) && secs=60
    log "waiting ${secs}s for the usage limit to reset: ${w#* }"
    sleep "$secs"
    continue
  fi
  log "step start"
  "$REPO/scripts/nightly_agents.sh"
  rc=$?
  if (( rc == 3 )); then
    log "step delivered nothing; waiting ${WAIT_IDLE}s"
    sleep "$WAIT_IDLE"
    continue
  fi
  log "step finished (exit $rc)"
  sleep "$GAP"
done
