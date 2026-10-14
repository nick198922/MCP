#!/bin/bash
# Continuous agent pipeline: one step at a time (scripts/nightly_agents.sh), with three brakes:
#  - plan usage: if the 5-hour or 7-day utilization is at or above TRADEDESK_AGENTS_MAX_USAGE (%), sleep until it resets
#  - review backlog: if TRADEDESK_AGENTS_MAX_OPEN agent branches wait for the owner, sleep and check again
#    (the agents' own work in progress is continued anyway: its branch does not wait for the owner; nor do the
#    doctor's fixes)
#  - idle: if there was nothing to do, sleep longer before the next step
# Failures call the doctor (scripts/agents_doctor.sh), which fixes what it can and delivers the fix as a PR; it also
# runs a daily health check at REPORT_HOUR. At most TRADEDESK_DOCTOR_MAX_PER_DAY doctor runs a day. Failures in a row
# space the next tries out (GAP, 2*GAP, 4*GAP, ... up to TRADEDESK_AGENTS_FAIL_MAX_WAIT_SECONDS).
# Telegram: each change of state once, and a silent project status (scripts/agents_status.py) every
# TRADEDESK_STATUS_EVERY_SECONDS outside the quiet hours TRADEDESK_STATUS_QUIET ("23-8").
# Runs as a systemd service (scripts/server/08-agents-loop.sh). Restart the service after changing this file.
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WT="${TRADEDESK_AGENTS_WORKTREE:-$(dirname "$REPO")/tradedesk-agents}"
LOG_DIR="${TRADEDESK_AGENTS_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tradedesk-agents}"
. "$REPO/scripts/agents_notify.sh"
MAX_USAGE="${TRADEDESK_AGENTS_MAX_USAGE:-95}"
MAX_OPEN="${TRADEDESK_AGENTS_MAX_OPEN:-2}"
WAIT_REVIEW="${TRADEDESK_AGENTS_REVIEW_WAIT_SECONDS:-1800}"
WAIT_IDLE="${TRADEDESK_AGENTS_IDLE_WAIT_SECONDS:-7200}"
GAP="${TRADEDESK_AGENTS_GAP_SECONDS:-300}"
REPORT_HOUR="${TRADEDESK_AGENTS_REPORT_HOUR:-9}"
DOCTOR_MAX="${TRADEDESK_DOCTOR_MAX_PER_DAY:-4}"
STATUS_EVERY="${TRADEDESK_STATUS_EVERY_SECONDS:-7200}"
STATUS_QUIET="${TRADEDESK_STATUS_QUIET:-23-8}"
FAIL_MAX_WAIT="${TRADEDESK_AGENTS_FAIL_MAX_WAIT_SECONDS:-14400}"
FAIL_ALERT=3
export TRADEDESK_AGENTS_WORKTREE="$WT" TRADEDESK_AGENTS_LOG_DIR="$LOG_DIR"
mkdir -p "$LOG_DIR"
# Also keep the loop's own log next to the run logs, where the doctor can read it.
exec > >(tee -a "$LOG_DIR/loop.log") 2>&1

log() { echo "$(date '+%F %T') $*"; }

# What the loop is doing, for the status message: "<epoch>\t<text>".
loop_state() { printf '%s\t%s\n' "$(date +%s)" "$1" >"$LOG_DIR/loop.state"; }
at() { date -d "+$1 seconds" '+%H:%M' 2>/dev/null || date -v+"$1"S '+%H:%M'; }

# Telegram only when the state changes (one message per wait, not one per check).
STATE_NOW=""
announce() {
  local key="$1"
  shift
  [[ "$key" == "$STATE_NOW" ]] && return 0
  STATE_NOW="$key"
  notify "$@"
}

# The same plan-usage figures as /usage. Prints the JSON; the token is never printed or logged.
usage_json() {
  local token
  token="$(jq -r '.claudeAiOauth.accessToken // empty' "$HOME/.claude/.credentials.json" 2>/dev/null)"
  [[ -z "$token" ]] && return 0
  printf 'header = "Authorization: Bearer %s"\n' "$token" | curl -s -m 15 -K - https://api.anthropic.com/api/oauth/usage \
    -H "anthropic-beta: oauth-2025-04-20" || true
}

# Prints "<seconds> <reason>" when usage is at or above MAX_USAGE, nothing when below or unknown.
usage_wait() {
  jq -r --argjson max "$MAX_USAGE" '
    [ to_entries[]
      | select(.key == "five_hour" or .key == "seven_day")
      | select((.value.utilization // -1) >= $max)
      | {k: .key, u: .value.utilization,
         r: (.value.resets_at | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601)} ]
    | if length == 0 then empty
      else (max_by(.r)) as $m | "\(($m.r - now | floor) + 300) \($m.k)=\($m.u)%"
      end' <<<"$1" 2>/dev/null || true
}

# Branches waiting for the owner; the doctor's fixes (agent/*-fix-*) and the agents' work in progress do not count.
work_branch() { cut -d' ' -f1 "$LOG_DIR/work.state" 2>/dev/null || true; }
open_branches() {
  local work
  work="$(work_branch)"
  git -C "$REPO" branch -r --no-merged origin/main --list 'origin/agent/*' | sed 's#^ *origin/##' | grep -v -- '-fix-' |
    grep -vxF -- "${work:-//none//}" || true
}

# The doctor, unless the usage limit is near or today's quota of doctor runs is used up.
doctor() {
  local day count
  day="$(date +%F)"
  count="$(awk -v d="$day" '$1 == d {print $2}' "$LOG_DIR/doctor.count" 2>/dev/null)"
  count="${count:-0}"
  if ((count >= DOCTOR_MAX)); then
    log "doctor skipped: $DOCTOR_MAX runs today already"
    return 0
  fi
  if [[ -n "$(usage_wait "$(usage_json)")" ]]; then
    log "doctor skipped: usage limit"
    return 0
  fi
  echo "$day $((count + 1))" >"$LOG_DIR/doctor.count"
  log "doctor: $1"
  loop_state "лікар оглядає систему"
  "$REPO/scripts/agents_doctor.sh" "$1" || log "doctor exited with $?"
}

# After a failure: wait longer the more failures there were in a row, and tell the owner once when they pile up.
fails=0
after_failure() {
  local wait="$GAP" i
  fails=$((fails + 1))
  for ((i = 1; i < fails && wait < FAIL_MAX_WAIT; i++)); do wait=$((wait * 2)); done
  ((wait > FAIL_MAX_WAIT)) && wait="$FAIL_MAX_WAIT"
  if ((fails == FAIL_ALERT)); then
    notify "🛑 **Агенти: $fails збої поспіль.** Далі пробую рідше (до разу на $((FAIL_MAX_WAIT / 3600)) год), наступна спроба о $(at "$wait"). Причину покаже \`agents-status.sh\`, або спитайте в темі «💬 Claude»."
  fi
  log "failure $fails in a row; next try in ${wait}s"
  loop_state "пауза після збою ($fails поспіль), наступна спроба о $(at "$wait")"
  sleep "$wait"
}

daily_check() {
  local day
  day="$(date +%F)"
  [[ "$(cat "$LOG_DIR/report.day" 2>/dev/null)" == "$day" ]] && return 0
  ((10#$(date +%H) >= REPORT_HOUR)) || return 0
  echo "$day" >"$LOG_DIR/report.day"
  doctor "daily health check"
}

quiet_now() {
  local h from to
  h=$((10#$(date +%H)))
  from="${STATUS_QUIET%-*}"
  to="${STATUS_QUIET#*-}"
  if ((from > to)); then ((h >= from || h < to)); else ((h >= from && h < to)); fi
}

# The status message runs beside the loop, so it keeps its schedule while a step takes hours.
status_ticker() {
  local now next
  while true; do
    now="$(date +%s)"
    next=$(((now / STATUS_EVERY + 1) * STATUS_EVERY))
    sleep $((next - now))
    quiet_now && continue
    python3 "$REPO/scripts/agents_status.py" --repo "$REPO" --worktree "$WT" --log-dir "$LOG_DIR" || log "status message failed"
  done
}

log "agents loop started (max usage ${MAX_USAGE}%, max open branches ${MAX_OPEN})"
last_start="$(cat "$LOG_DIR/loop.started" 2>/dev/null || echo 0)"
if (($(date +%s) - last_start > 3600)); then
  notify "▶️ **Цикл агентів запущено.** Пауза при використанні плану ≥ ${MAX_USAGE}%, не більше ${MAX_OPEN} гілок на вашому перегляді; статус проєкту кожні $((STATUS_EVERY / 3600)) год (крім ${STATUS_QUIET} год)." --silent
fi
date +%s >"$LOG_DIR/loop.started"
status_ticker &

while true; do
  daily_check
  git -C "$REPO" fetch -q origin || log "fetch failed"
  leftovers=0
  [[ -n "$(git -C "$WT" status --porcelain 2>/dev/null)" || -n "$(work_branch)" ]] && leftovers=1
  open="$(open_branches)"
  n_open="$(grep -c . <<<"$open")"
  if ((n_open >= MAX_OPEN && leftovers == 0)); then
    log "waiting ${WAIT_REVIEW}s: $n_open agent branches await the owner's review"
    loop_state "чекають на ваш перегляд: $n_open гілки"
    buttons=()
    while IFS= read -r b; do
      [[ -n "$b" ]] && buttons+=(--button "🔀 ${b#agent/}|https://github.com/nick198922/MCP/compare/main...$b?expand=1")
    done <<<"$open"
    announce "review:$open" "⏸ **Агенти чекають на ваш перегляд** ($n_open гілки). Злийте або закрийте PR, і вони продовжать." ${buttons[@]+"${buttons[@]}"}
    sleep "$WAIT_REVIEW"
    continue
  fi
  w="$(usage_wait "$(usage_json)")"
  if [[ -n "$w" ]]; then
    secs="${w%% *}"
    ((secs < 60)) && secs=60
    log "waiting ${secs}s for the usage limit to reset: ${w#* }"
    loop_state "пауза: ліміт Claude (${w#* }), продовжать о $(at "$secs")"
    announce "usage:${w#* }" "⏸ **Агенти на паузі:** використано ${w#* } ліміту плану. Продовжать приблизно о $(at "$secs")." --silent
    sleep "$secs"
    continue
  fi
  STATE_NOW=""
  log "step start"
  loop_state "крок триває"
  "$REPO/scripts/nightly_agents.sh"
  rc=$?
  # Run logs only (<stamp>.log), not the doctor's, the rescue checks', the loop's or the Telegram bridge's.
  last_log="$(ls -t "$LOG_DIR"/20??-??-??_??????.log 2>/dev/null | head -1)"
  last_status="$(tail -1 "$LOG_DIR/history.tsv" 2>/dev/null | cut -f2)"
  ((rc == 0 || rc == 3 || rc == 4)) && fails=0
  case $rc in
    0)
      log "step finished"
      if [[ "$last_status" == "stuck" ]]; then
        doctor "The agents could not finish one step in the allowed number of runs; it was handed to the owner as it is. Find why the runs kept ending without delivery. Run log: $last_log"
      fi
      loop_state "пауза між кроками, наступний о $(at "$GAP")"
      sleep "$GAP"
      ;;
    3)
      log "nothing to do; waiting ${WAIT_IDLE}s"
      loop_state "роботи немає, наступна перевірка о $(at "$WAIT_IDLE")"
      sleep "$WAIT_IDLE"
      ;;
    4)
      log "work continues; next run after ${GAP}s"
      loop_state "продовжать роботу о $(at "$GAP")"
      sleep "$GAP"
      ;;
    5)
      log "claude failed"
      doctor "claude exited with an error during an agent step. Run log: $last_log"
      after_failure
      ;;
    *)
      log "step script failed (exit $rc)"
      ((fails == 0)) && notify "❌ **Скрипт кроку агентів упав** (код $rc). Викликаю лікаря. Лог: $last_log"
      doctor "scripts/nightly_agents.sh itself failed with exit code $rc. Run log: $last_log"
      after_failure
      ;;
  esac
done
