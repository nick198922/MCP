#!/bin/bash
# Continuous agent pipeline: one step at a time (scripts/nightly_agents.sh), with three brakes:
#  - plan usage: if the 5-hour or 7-day utilization is at or above TRADEDESK_AGENTS_MAX_USAGE (%), sleep until it resets
#  - weekly pace: the agents spend the week evenly, (100 - TRADEDESK_AGENTS_OWNER_RESERVE) % over 7 days from the
#    reset (about 1/7 a day), the rest kept for the owner's sessions; ahead of that schedule they sleep until it
#    catches up, unless an owner-priority card is open: then they take only those cards (owner 27.10: «хай беруть
#    пріоритетні задачі не дивлячись на ліміти, до 97 %»; the agents had idled 40 h on the pace brake)
#    Both pace brakes (this one and OWNER_ONLY_AT) are off while the owner's boost runs: scripts/agents_boost.sh writes
#    an end time (epoch seconds) into $LOG_DIR/boost-until; the MAX_USAGE brake stays, so the plan is never driven
#    into its hard limit
#  - review backlog: if TRADEDESK_AGENTS_MAX_OPEN agent branches wait for the owner, sleep and check again
#    (the agents' own work in progress is continued anyway: its branch does not wait for the owner; nor do the
#    doctor's fixes)
#  - idle: if there was nothing to do, sleep longer before the next step
# Failures call the doctor (scripts/agents_doctor.sh), which fixes what it can and delivers the fix as a PR; it also
# runs a daily health check at REPORT_HOUR. At most TRADEDESK_DOCTOR_MAX_PER_DAY doctor runs a day for failures (the
# daily check does not count), and the same failure is examined once a day. Failures in a row
# space the next tries out (GAP, 2*GAP, 4*GAP, ... up to TRADEDESK_AGENTS_FAIL_MAX_WAIT_SECONDS).
# Telegram: each change of state once, and a silent project status (scripts/agents_status.py) every
# TRADEDESK_STATUS_EVERY_SECONDS outside the quiet hours TRADEDESK_STATUS_QUIET ("23-8").
# Runs as a systemd service (scripts/server/08-agents-loop.sh). A new version of this file (the server pulls main every
# night) takes over by itself between steps.
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WT="${TRADEDESK_AGENTS_WORKTREE:-$(dirname "$REPO")/tradedesk-agents}"
LOG_DIR="${TRADEDESK_AGENTS_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/tradedesk-agents}"
. "$REPO/scripts/agents_notify.sh"
. "$REPO/scripts/agents_usage.sh"
MAX_USAGE="${TRADEDESK_AGENTS_MAX_USAGE:-97}"
OWNER_ONLY_AT="${TRADEDESK_AGENTS_OWNER_ONLY_AT:-80}" # weekly %: from here only the owner's cards (Пріоритет: власник)
WAIT_BUDGET=3600
RESERVE="${TRADEDESK_AGENTS_OWNER_RESERVE:-15}" # weekly % never planned for the agents
PACE_SLACK="${TRADEDESK_AGENTS_PACE_SLACK:-3}"  # weekly % the agents may run ahead of the even schedule
MAX_OPEN="${TRADEDESK_AGENTS_MAX_OPEN:-2}"
WAIT_REVIEW="${TRADEDESK_AGENTS_REVIEW_WAIT_SECONDS:-1800}"
WAIT_IDLE="${TRADEDESK_AGENTS_IDLE_WAIT_SECONDS:-1800}"
GAP="${TRADEDESK_AGENTS_GAP_SECONDS:-300}"
REPORT_HOUR="${TRADEDESK_AGENTS_REPORT_HOUR:-9}"
DOCTOR_MAX="${TRADEDESK_DOCTOR_MAX_PER_DAY:-4}"
STATUS_EVERY="${TRADEDESK_STATUS_EVERY_SECONDS:-7200}"
STATUS_QUIET="${TRADEDESK_STATUS_QUIET:-23-8}"
FAIL_MAX_WAIT="${TRADEDESK_AGENTS_FAIL_MAX_WAIT_SECONDS:-14400}"
FAIL_ALERT=3
BOOST_FILE="$LOG_DIR/boost-until"
export TRADEDESK_AGENTS_WORKTREE="$WT" TRADEDESK_AGENTS_LOG_DIR="$LOG_DIR"
mkdir -p "$LOG_DIR"
# Also keep the loop's own log next to the run logs, where the doctor can read it (once: a re-exec keeps the tee).
if [[ -z "${TRADEDESK_LOOP_TEED:-}" ]]; then
  export TRADEDESK_LOOP_TEED=1
  exec > >(tee -a "$LOG_DIR/loop.log") 2>&1
fi
# The loop restarts itself when this script or a helper it sources changes (they are read once, at start).
self_sum() { cat "$0" "$REPO/scripts/agents_usage.sh" "$REPO/scripts/agents_notify.sh" 2>/dev/null | cksum; }
SELF="$(self_sum)"

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

# Prints "<seconds> <used>% <planned>%" when the weekly usage runs ahead of the even schedule, nothing otherwise or
# when unknown: planned = (100 - RESERVE) * (share of the week since the reset) + PACE_SLACK + the weekly audit's
# cost this week (\$LOG_DIR/audit.cost, "<resets_at> <percent>", written by scripts/agents_audit.sh), so the agents do
# not pay for the audit with a day of pause; <seconds> is when the schedule reaches the usage (past the reset: 5 minutes
# after it).
pace_wait() {
  local audit
  audit="$(head -c 100 "$LOG_DIR/audit.cost" 2>/dev/null || true)"
  jq -r --argjson res "$RESERVE" --argjson base "$PACE_SLACK" --arg audit "$audit" '
    .seven_day as $w
    | ($audit | split(" ")) as $a
    | ($base + (if ($a | length) == 2 and $a[0] == $w.resets_at then ($a[1] | tonumber? // 0) else 0 end)) as $slack
    | ($w.utilization // empty) as $u
    | ($w.resets_at | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601) as $r
    | 604800 as $week
    | ([[1 - ($r - now) / $week, 0] | max, 1] | min) as $f
    | ((100 - $res) * $f + $slack) as $plan
    | if $u <= $plan then empty
      else (($u - $slack) / (100 - $res)) as $need
        | (if $need >= 1 then $r - now + 300 else ($need - $f) * $week + 60 end | floor) as $s
        | "\($s) \($u)% \($plan | floor)%"
      end' <<<"$1" 2>/dev/null || true
}

# True while the owner's boost (scripts/agents_boost.sh) runs: an end time in the future.
boost_on() {
  local u
  u="$(head -c 20 "$BOOST_FILE" 2>/dev/null | tr -cd '0-9')"
  [[ -n "$u" ]] && ((u > $(date +%s)))
}

# Branches waiting for the owner; the doctor's fixes (agent/*-fix-*) and the agents' work in progress do not count.
work_branch() { cut -d' ' -f1 "$LOG_DIR/work.state" 2>/dev/null || true; }
# Owner-priority cards on main that still need work (accepted, testing or proposed).
owner_cards_open() {
  local f
  git -C "$REPO" grep -l -e '^Пріоритет: власник' origin/main -- 'docs/ideas/IDEA-*.md' 2>/dev/null | sed 's#^origin/main:##' |
    while read -r f; do
      git -C "$REPO" show "origin/main:$f" 2>/dev/null | grep -qE '^Статус: (accepted|testing|proposed)' && echo "$f"
    done || true
}
# Unfinished work, not a handover: parked, or its last outcome in history.tsv set it aside (parked, continues, unfinished).
wip_branch() {
  grep -q -- "^$1 " "$LOG_DIR/parked.state" 2>/dev/null && return 0
  case "$(grep -F "\`$1\`" "$LOG_DIR/history.tsv" 2>/dev/null | tail -1 | cut -f2)" in
  parked | continues | unfinished) return 0 ;;
  esac
  return 1
}
# Agent branches that wait to be merged: not merged, not a doctor's fix, not work in progress.
open_branches() {
  local work b
  work="$(work_branch)"
  git -C "$REPO" branch -r --no-merged origin/main --list 'origin/agent/*' | sed 's#^ *origin/##' | grep -v -- '-fix-' |
    grep -vxF -- "${work:-//none//}" | while read -r b; do wip_branch "$b" || echo "$b"; done || true
}

# doctor "<what happened>" ["<failure signature>"]: the doctor, unless the usage limit is near. For a failure (with a
# signature: the outcome line, paths left out) also unless today's quota is used up or the same failure was examined
# today already: a second look at an unchanged cause only burns tokens.
doctor() {
  local day count sig=""
  day="$(date +%F)"
  if [[ -n "${2:-}" ]]; then
    sig="$(printf '%s' "$2" | sed 's#/[^ ]*##g' | cksum | cut -d' ' -f1)"
    if grep -qx "$day $sig" "$LOG_DIR/doctor.seen" 2>/dev/null; then
      log "doctor skipped: this failure was examined today"
      return 0
    fi
    count="$(awk -v d="$day" '$1 == d {print $2}' "$LOG_DIR/doctor.count" 2>/dev/null)"
    count="${count:-0}"
    if ((count >= DOCTOR_MAX)); then
      log "doctor skipped: $DOCTOR_MAX runs for failures today already"
      return 0
    fi
  fi
  if [[ -n "$(usage_wait "$(usage_json)")" ]]; then
    log "doctor skipped: usage limit"
    return 0
  fi
  if [[ -n "$sig" ]]; then
    echo "$day $((count + 1))" >"$LOG_DIR/doctor.count"
    { grep "^$day " "$LOG_DIR/doctor.seen" 2>/dev/null; echo "$day $sig"; } >"$LOG_DIR/doctor.seen.tmp"
    mv "$LOG_DIR/doctor.seen.tmp" "$LOG_DIR/doctor.seen"
  fi
  log "doctor: $1"
  loop_state "лікар оглядає систему"
  "$REPO/scripts/agents_doctor.sh" "$1" || log "doctor exited with $?"
}

# Idle watchdog (owner 30.10: "I want no idle time; when it happens, fix it yourself instead of waiting for me to ask"):
# two idle runs in a row (nothing to take, or a delivery that is only an idle summary) call the doctor in idle mode
# (.claude/doctor.md, "Idle") to find what blocks the open work and remove it; at most once per IDLE_DOCTOR_GAP, outside
# the failure quota.
idles=0
IDLE_DOCTOR_GAP="${TRADEDESK_IDLE_DOCTOR_GAP_SECONDS:-10800}"
idle_check() { # idle_check <1 = idle run | 0 = real work>
  if (($1 == 0)); then
    idles=0
    return 0
  fi
  idles=$((idles + 1))
  ((idles >= 2)) || return 0
  local last now
  last="$(cat "$LOG_DIR/idle_doctor.ts" 2>/dev/null)"
  now="$(date +%s)"
  ((now - ${last:-0} >= IDLE_DOCTOR_GAP)) || return 0
  echo "$now" >"$LOG_DIR/idle_doctor.ts"
  idles=0
  doctor "IDLE: the agents found nothing to take in $2 runs in a row. Follow the Idle section of your instructions. Latest run log: $last_log"
}

# Idle wait that ends early once an agent branch is merged (owner 30.10: the agents idled half an hour while the S3
# passport waited 15 min for the trader review; the card it held is free as soon as it merges).
# Also ends when main gets new work: a card or an answer in docs/ideas/ or docs/QUESTIONS.md (owner 31.10: his new card
# from the chat session waited half an hour, it is not an agent branch).
work_tree() { git -C "$REPO" rev-parse -q origin/main:docs/ideas origin/main:docs/QUESTIONS.md 2>/dev/null | tr '\n' ' '; }
idle_wait() { # idle_wait <seconds>
  local left="$1" before now tree
  before="$(open_branches | wc -l)"
  tree="$(work_tree)"
  while ((left > 0)); do
    sleep 60
    left=$((left - 60))
    git -C "$REPO" fetch -q --no-write-fetch-head origin 2>/dev/null || continue
    now="$(open_branches | wc -l)"
    if ((now < before)); then
      log "an agent branch was merged: next step now"
      return 0
    fi
    if [[ "$(work_tree)" != "$tree" ]]; then
      log "new cards or answers on main: next step now"
      return 0
    fi
    before="$now"
  done
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

# The weekly audit (scripts/agents_audit.sh) gets the fresh weekly limit first: no new steps while it runs, nor in
# the minutes before its timer (Tuesday 19:45-20:03 Kyiv), so a step does not slip in just before it starts.
audit_running() {
  local pid hm
  pid="$(cat "$LOG_DIR/audit.pid" 2>/dev/null)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && return 0
  [[ "$(TZ=Europe/Kyiv date +%u)" == 2 ]] || return 1
  hm="$(TZ=Europe/Kyiv date +%H%M)"
  ((10#$hm >= 1945 && 10#$hm < 2003))
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
TICKER=$!

while true; do
  if [[ "$(self_sum)" != "$SELF" ]]; then
    log "this script changed; continuing with the new version"
    kill "$TICKER" 2>/dev/null
    exec "$0" "$@"
  fi
  if audit_running; then
    log "waiting 300s: the weekly audit runs first"
    loop_state "пауза: триває тижневий аудит"
    announce "audit" "⏸ **Агенти чекають:** спершу тижневий аудит, потім продовжать самі." --silent
    sleep 300
    continue
  fi
  daily_check
  git -C "$REPO" fetch -q --no-write-fetch-head origin || log "fetch failed"
  leftovers=0
  [[ -n "$(git -C "$WT" status --porcelain 2>/dev/null)" || -n "$(work_branch)" ]] && leftovers=1
  open="$(open_branches)"
  n_open="$(grep -c . <<<"$open")"
  if ((n_open >= MAX_OPEN && leftovers == 0)); then
    log "waiting ${WAIT_REVIEW}s: $n_open agent branches await the owner's review"
    loop_state "чекають на злиття: $n_open гілки"
    buttons=()
    while IFS= read -r b; do
      [[ -n "$b" ]] && buttons+=(--button "🔀 ${b#agent/}|https://github.com/nick198922/MCP/compare/main...$b?expand=1")
    done <<<"$open"
    announce "review:$open" "⏸ **Агенти чекають, поки зіллються їхні PR** ($n_open гілки). Сторож PR кожні 15 хв перезапускає впалий CI і доливає зелені; якщо CI червоний і після перезапуску, прийде 🔴." ${buttons[@]+"${buttons[@]}"}
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
  p=""
  boost_on || p="$(pace_wait "$(usage_json)")"
  export TRADEDESK_AGENTS_OWNER_ONLY=""
  if [[ -n "$p" && -n "$(owner_cards_open)" ]]; then
    log "ahead of the weekly schedule (${p#* }), owner-priority cards open: taking only those"
    TRADEDESK_AGENTS_OWNER_ONLY="ahead of the weekly schedule (${p#* })"
    p=""
  fi
  if [[ -n "$p" ]]; then
    secs="${p%% *}"
    read -r used plan <<<"${p#* }"
    resume="$(at "$secs")"
    # Sleep at most an hour at a time: the owner's own sessions move the figures, and a new script version takes over.
    ((secs > 3600)) && secs=3600
    log "waiting ${secs}s: weekly usage ${used} ahead of the even schedule ${plan}"
    loop_state "пауза за графіком: тиждень ${used}, за планом ${plan}; продовжать приблизно о ${resume}"
    announce "pace" "⏸ **Агенти на паузі за графіком ліміту:** тиждень ${used}, за рівномірним планом ${plan} (${RESERVE}% тримаю для вас). Продовжать приблизно о ${resume}." --silent
    sleep "$secs"
    continue
  fi
  week="$(usage_now | awk '{print $2}')"
  if ((leftovers == 0)) && ! boost_on && [[ -n "$week" ]] && awk -v w="$week" -v m="$OWNER_ONLY_AT" 'BEGIN { exit !(w >= m) }' &&
    [[ -z "$(owner_cards_open)" ]]; then
    log "waiting ${WAIT_BUDGET}s: weekly usage ${week}% >= ${OWNER_ONLY_AT}%, kept for the owner's cards, none open"
    loop_state "бюджет тижня (${week}%) лише для ваших карток; їх немає, наступна перевірка о $(at "$WAIT_BUDGET")"
    sleep "$WAIT_BUDGET"
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
  last_outcome="$(tail -1 "$LOG_DIR/history.tsv" 2>/dev/null | cut -f2-3)"
  ((rc == 0 || rc == 3 || rc == 4)) && fails=0
  case $rc in
    0)
      log "step finished"
      if [[ "$last_outcome" == *-idle* ]]; then idle_check 1 2; else idle_check 0; fi
      if [[ "$last_status" == "stuck" ]]; then
        doctor "The agents could not finish one step in the allowed number of runs; it was handed to the owner as it is. Find why the runs kept ending without delivery. Run log: $last_log" "$last_outcome"
      fi
      loop_state "пауза між кроками, наступний о $(at "$GAP")"
      sleep "$GAP"
      ;;
    3)
      log "nothing to do; waiting ${WAIT_IDLE}s"
      idle_check 1 2
      loop_state "роботи немає, наступна перевірка о $(at "$WAIT_IDLE") або одразу після злиття гілки агентів"
      idle_wait "$WAIT_IDLE"
      ;;
    4)
      log "work continues; next run after ${GAP}s"
      loop_state "продовжать роботу о $(at "$GAP")"
      sleep "$GAP"
      ;;
    5)
      log "claude failed"
      doctor "claude exited with an error during an agent step. Run log: $last_log" "$last_outcome"
      after_failure
      ;;
    *)
      log "step script failed (exit $rc)"
      ((fails == 0)) && notify "❌ **Скрипт кроку агентів упав** (код $rc). Викликаю лікаря. Лог: $last_log"
      doctor "scripts/nightly_agents.sh itself failed with exit code $rc. Run log: $last_log" "script exit $rc"
      after_failure
      ;;
  esac
done
